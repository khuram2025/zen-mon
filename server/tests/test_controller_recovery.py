"""Replay controller restarts through real evaluators on disposable PostgreSQL."""
import asyncio
from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import text

from app.services import controller_recovery as cr
from app.services import host_alert_service as has
from app.services import server_availability as sa
from app.services import server_health_service as shs
from app.services import sensor_health_service as sshs
from pg_fixture import pg_cluster, pg_db  # noqa: F401
from test_server_availability_alerts import (  # noqa: F401
    world, session, add_server, add_rule, beat, run, incidents, T0,
)


def recovery(w, *, ready=T0, now=T0, deadline=T0 + cr.HARD_LIMIT_S):
    state = cr.Recovery(T0, ready, deadline, now, "restart-1")
    w.db.execute("INSERT INTO system_settings(key,value) VALUES($1,$2::jsonb) "
                 "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", cr.KEY, state.__dict__)
    return state


def test_reconnected_agent_uses_normal_threshold_during_fleet_recovery():
    state = cr.Recovery(T0, T0, T0 + cr.HARD_LIMIT_S, T0 + 200, "boot")
    fresh = datetime.fromtimestamp(T0 + 1, timezone.utc)
    old = datetime.fromtimestamp(T0 - 1000, timezone.utc)
    assert state.hold_missing(old, T0 + 200, 180)
    assert not state.hold_missing(fresh, T0 + 200, 180)
    assert not state.hold_missing(old, T0 + cr.HARD_LIMIT_S, 180)


@pytest.mark.asyncio
async def test_healthy_fleet_reconnects_silently_and_disconnected_host_pages_once(world):
    w = world
    add_rule(w, "ping_status", name="Device Down")
    healthy = [add_server(w, f"healthy-{i}", heartbeat=T0 - 900) for i in range(20)]
    down = add_server(w, "disconnected", heartbeat=T0 - 900)
    recovery(w, now=T0 + 200)
    await run(w, T0 + 200)
    assert w.sent == []
    assert all(incidents(w, sid) == [] for sid in [*healthy, down])
    # Maximum communication retry + heartbeat scheduling still gets no false page.
    for sid in healthy:
        beat(w, sid, T0 + cr.RECONNECT_S - 1)
    recovery(w, now=T0 + cr.RECONNECT_S + 179)
    await run(w, T0 + cr.RECONNECT_S + 179)
    assert w.sent == []
    for sid in healthy:
        beat(w, sid, T0 + cr.RECONNECT_S + 181)
    recovery(w, now=T0 + cr.RECONNECT_S + 181)
    await run(w, T0 + cr.RECONNECT_S + 181)
    assert len(w.sent) == 1 and w.sent[0]["hostname"] == "disconnected"
    await run(w, T0 + cr.RECONNECT_S + 182)
    assert len(w.sent) == 1
    assert all(incidents(w, sid) == [] for sid in healthy)


@pytest.mark.asyncio
async def test_real_outage_after_reconnect_does_not_wait_for_fleet_grace(world):
    w = world
    add_rule(w, "ping_status")
    sid = add_server(w, "fails-again", heartbeat=T0 + 10)
    recovery(w, now=T0 + 191)
    await run(w, T0 + 191)
    assert len(w.sent) == 1 and incidents(w, sid)[0].status == "active"


@pytest.mark.asyncio
async def test_existing_incident_is_preserved_and_only_recovers_on_fresh_heartbeat(world):
    w = world
    add_rule(w, "ping_status")
    sid = add_server(w, "existing", heartbeat=T0 - 500)
    await run(w, T0 - 1)
    assert len(w.sent) == 1
    recovery(w, now=T0 + 100)
    await run(w, T0 + 100)
    assert len(w.sent) == 1 and incidents(w, sid)[0].status == "active"
    assert incidents(w, sid)[0].metadata["data_state"] == "unknown"
    beat(w, sid, T0 + 101)
    await run(w, T0 + 102)
    assert len(w.sent) == 2 and w.sent[-1]["is_recovery"]


@pytest.mark.asyncio
async def test_last_moment_heartbeat_cancels_send_and_suppresses_unseen_recovery(world, monkeypatch):
    w = world
    add_rule(w, "ping_status")
    sid = add_server(w, "just-reconnected", heartbeat=T0 - 500)
    original = has.create_server_alert
    async def reconnect_before_commit(db, *args, **kwargs):
        result = await original(db, *args, **kwargs)
        await db.execute(text("UPDATE agents SET last_heartbeat_at=to_timestamp(:t) WHERE server_id=CAST(:sid AS uuid)"),
                         {"t": T0, "sid": sid})
        return result
    monkeypatch.setattr(has, "create_server_alert", reconnect_before_commit)
    await run(w, T0)
    assert w.sent == []
    await run(w, T0 + 1)
    assert w.sent == [] and incidents(w, sid)[0].status == "resolved"


@pytest.mark.asyncio
async def test_multiworker_start_and_restart_loop_keep_one_hard_deadline(pg_db):
    async def start(token, now, ready):
        async with session(pg_db) as db:
            return await cr.transition(db, token=token, now=now, ready=ready)
    states = await asyncio.gather(start("boot-1", T0, False), start("boot-1", T0, False))
    assert states[0] == states[1]
    first = await start("boot-1", T0 + 1, True)
    same = await start("boot-1", T0 + 5, False)
    assert same.ready_at == first.ready_at
    restarted = await start("boot-2", T0 + 100, True)
    assert restarted.deadline == first.deadline
    expired = await start("boot-3", T0 + cr.HARD_LIMIT_S + 10, True)
    assert expired.deadline == first.deadline
    assert not expired.hold_missing(None, T0 + cr.HARD_LIMIT_S + 10, 180)


@pytest.mark.asyncio
async def test_planned_maintenance_and_gap_detection_persist(pg_db):
    async with session(pg_db) as db:
        planned = await cr.transition(db, token="ota", now=T0, planned=True)
    async with session(pg_db) as db:
        assert (await cr.load(db)) == planned
        assert planned.hold_missing(None, T0 + 20, 180)
        ready = await cr.transition(db, token="api-boot", now=T0 + 30, ready=True)
        gap = await cr.transition(db, token="api-boot", now=T0 + 150, ready=True)
        assert gap.ready_at == T0 + 150 and gap.deadline == ready.deadline


@pytest.mark.asyncio
async def test_staleness_sweep_does_not_invent_offline_alerts_during_restart(world):
    w = world
    now = datetime.now(timezone.utc).timestamp()
    sid = add_server(w, "sweep-reconnect", heartbeat=now - 1200)
    recovery(w, ready=now, now=now, deadline=now + cr.HARD_LIMIT_S)
    async with session(w.db) as db:
        await shs.sweep_stale(db)
    assert w.db.scalar("SELECT status FROM agents WHERE server_id=$1", sid) == "online"
    assert incidents(w, sid) == []
    # Hard deadline is fail-open even if the readiness lease never recovered.
    recovery(w, ready=None, now=now - 2000, deadline=now - 1)
    async with session(w.db) as db:
        await shs.sweep_stale(db)
    assert w.db.scalar("SELECT status FROM agents WHERE server_id=$1", sid) == "offline"
    assert len(incidents(w, sid)) == 1


@pytest.mark.asyncio
async def test_sensor_timeout_query_obeys_same_recovery_gate(world):
    w = world
    now = datetime.now(timezone.utc).timestamp()
    recovery(w, ready=now, now=now, deadline=now + cr.HARD_LIMIT_S)
    async with session(w.db) as db:
        result = await sshs.sweep_sensor_health(db)
    assert result["offline"] == 0


def test_signed_update_records_window_before_stopping_services():
    from pathlib import Path
    source = (Path(__file__).parents[2] / "scripts/build-release.py").read_text()
    assert source.index('"code/scripts/ota-monitoring-maintenance.py"') < source.index('"type": "stop_services"')
