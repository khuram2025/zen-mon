"""Remote-sensor device status through the shared state machine (DESIGN "Remote-sensor writer").

The real ``post_ping_results`` (authentication, sensor lock, ledger, freshness guard, ownership,
outbox) and the real outbox dispatcher and sensor-offline sweep run against the private PostgreSQL
cluster from ``pg_fixture`` (never the appliance database). ClickHouse, the sensor quota (Redis), the
live Redis stream and the alert engine are replaced by recorders, so nothing leaves the test; one
dispatcher test runs the real alert engine (no rules or channels, so it only resolves alert rows).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import alert_engine, sensor_api
from app.schemas.sensor import PingResultsBatch
from app.services import device_status_machine as dsm
from app.services import sensor_health_service
from pg_fixture import PgDatabase, pg_cluster, pg_db  # noqa: F401

API_KEY = "zps_device_status_machine_test"
UTC = timezone.utc

# The real publisher and alert engine, kept before the autouse fixture swaps in recorders.
REAL_PUBLISH = sensor_api._publish_status_change
REAL_EVALUATE = alert_engine.evaluate_status_change

LEGACY = {"degraded_confirm_checks": 1, "degraded_window_checks": 1, "degraded_clear_checks": 1,
          "degraded_rtt_clear_ms": 100, "degraded_loss_clear_pct": 10, "flap_detection_enabled": False}


@pytest.fixture(autouse=True)
def recorders(monkeypatch):
    """ClickHouse inserts, the live Redis stream and the alert engine, recorded in memory."""
    clickhouse: list[tuple[str, list]] = []
    published: list[dict] = []
    evaluated: list = []

    async def insert_clickhouse(table, rows, column_names):
        clickhouse.append((table, [dict(zip(column_names, row)) for row in rows]))

    async def no_quota(*_args, **_kwargs):
        return None

    async def publish(item):
        published.append(dict(item))

    async def evaluate(event, db):
        evaluated.append(event)
        return {}

    monkeypatch.setattr(sensor_api, "_insert_clickhouse", insert_clickhouse)
    monkeypatch.setattr(sensor_api, "_limit_sensor_request", no_quota)
    monkeypatch.setattr(sensor_api, "_publish_status_change", publish)
    monkeypatch.setattr(alert_engine, "evaluate_status_change", evaluate)
    return SimpleNamespace(clickhouse=clickhouse, published=published, evaluated=evaluated)


# ------------------------------------------------------------------ world

def add_sensor(db: PgDatabase, name: str | None = None) -> uuid.UUID:
    return db.scalar("INSERT INTO sensors (name, status, api_key_hash, last_heartbeat_at, last_seen_at) "
                     "VALUES ($1, 'online', $2, now(), now()) RETURNING id",
                     name or f"sensor-{uuid.uuid4().hex[:8]}", hashlib.sha256(API_KEY.encode()).hexdigest())


def add_device(db: PgDatabase, sensor: uuid.UUID | None, status: str = "up", *, interval: int = 60) -> uuid.UUID:
    octets = uuid.uuid4().bytes
    device = db.scalar("INSERT INTO devices (hostname, ip_address, status, ping_interval) "
                       "VALUES ($1, $2::inet, $3, $4) RETURNING id",
                       f"dev-{octets.hex()[:6]}", f"10.{octets[0]}.{octets[1]}.{octets[2] or 1}", status, interval)
    if sensor is not None:
        assign(db, sensor, device)
    return device


def assign(db: PgDatabase, sensor: uuid.UUID, device: uuid.UUID) -> None:
    db.execute("INSERT INTO sensor_assignments (sensor_id, target_type, target_id) VALUES ($1, 'device', $2)",
               sensor, device)


def set_policy(db: PgDatabase, doc: dict) -> None:
    db.execute("INSERT INTO system_settings (key, value) VALUES ('monitoring', $1) "
               "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", doc)


def owner_of(db: PgDatabase, device: uuid.UUID) -> uuid.UUID:
    return db.scalar("SELECT sensor_id FROM device_polling_owner WHERE device_id = $1", device)


def sample(device, ts: datetime, *, up: bool = True, rtt: float = 10.0, loss: float | None = None) -> dict:
    return {"device_id": str(device), "timestamp": ts.isoformat(), "is_up": up,
            "rtt_ms": rtt if up else None, "packet_loss": (0.0 if up else 1.0) if loss is None else loss,
            "packets_sent": 3, "packets_received": 3 if up else 0}


def ok(device, ts):
    return sample(device, ts, rtt=10.0)


def slow(device, ts):
    return sample(device, ts, rtt=250.0)


def fail(device, ts):
    return sample(device, ts, up=False)


def recent(seconds_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds_ago)


async def session_for(db: PgDatabase):
    engine = create_async_engine(db.url, poolclass=NullPool)
    return engine, AsyncSession(engine, expire_on_commit=False)


async def ingest(db: PgDatabase, sensor: uuid.UUID, items: list[dict], *, key: str | None = None) -> dict:
    body = PingResultsBatch(idempotency_key=key or f"batch-{uuid.uuid4().hex}", items=items)
    engine, session = await session_for(db)
    try:
        async with session:
            return await sensor_api.post_ping_results(
                body, request=None, db=session, x_sensor_id=str(sensor), authorization=f"Bearer {API_KEY}")
    finally:
        await engine.dispose()


async def dispatch(db: PgDatabase) -> dict:
    engine, session = await session_for(db)
    try:
        async with session:
            return await sensor_health_service.dispatch_sensor_transition_outbox(session, limit=20)
    finally:
        await engine.dispose()


async def sweep(db: PgDatabase) -> dict:
    engine, session = await session_for(db)
    try:
        async with session:
            return await sensor_health_service.sweep_sensor_health(session)
    finally:
        await engine.dispose()


def device_row(db: PgDatabase, device) -> dict:
    row = db.query("SELECT status, is_flapping, flapping_since, flap_kind, status_changed_at, last_seen, last_rtt_ms "
                   "FROM devices WHERE id = $1", device)[0]
    return dict(zip(("status", "is_flapping", "flapping_since", "flap_kind", "status_changed_at", "last_seen",
                     "last_rtt_ms"), row))


def state_row(db: PgDatabase, device) -> dict | None:
    rows = db.query("SELECT owner, hard_status, in_maintenance, fail_streak, window_bits, window_len, status_since, "
                    "last_sample_at, perf_flapping FROM device_status_state WHERE device_id = $1", device)
    if not rows:
        return None
    return dict(zip(("owner", "hard_status", "in_maintenance", "fail_streak", "window_bits", "window_len",
                     "status_since", "last_sample_at", "perf_flapping"), rows[0]))


def outbox(db: PgDatabase, device=None) -> list[dict]:
    rows = db.query("SELECT payload, processed_at, attempts, last_error FROM sensor_transition_outbox "
                    "WHERE transition_type = 'device' AND ($1::uuid IS NULL OR entity_id = $1) "
                    "ORDER BY created_at, id", device)
    return [{"payload": r[0], "processed_at": r[1], "attempts": r[2], "last_error": r[3]} for r in rows]


# ------------------------------------------------------------------ confirmation end to end

@pytest.mark.asyncio
async def test_one_slow_sample_is_a_blip_and_three_of_five_confirm_degraded(pg_db, recorders):
    sensor = add_sensor(pg_db, "edge-1")
    device = add_device(pg_db, sensor, "up")
    t = recent(70)

    await ingest(pg_db, sensor, [ok(device, t)])
    assert device_row(pg_db, device)["status"] == "up"
    # Seeded silently from devices.status: no transition for a device that simply stays up.
    assert state_row(pg_db, device)["owner"] == str(sensor) and outbox(pg_db) == []

    await ingest(pg_db, sensor, [slow(device, t + timedelta(seconds=10))])
    assert device_row(pg_db, device)["status"] == "up"      # one slow reply is not a status
    assert state_row(pg_db, device)["window_bits"] == 0b01
    assert outbox(pg_db) == []

    await ingest(pg_db, sensor, [ok(device, t + timedelta(seconds=20)), slow(device, t + timedelta(seconds=30))])
    assert device_row(pg_db, device)["status"] == "up"      # 2 of the last 4
    assert outbox(pg_db) == []

    confirmed_at = t + timedelta(seconds=40)
    await ingest(pg_db, sensor, [slow(device, confirmed_at)])
    row = device_row(pg_db, device)
    assert row["status"] == "degraded"                      # 3 of the last 5
    assert row["status_changed_at"] == confirmed_at and row["last_seen"] == confirmed_at
    assert row["last_rtt_ms"] == 250.0 and row["is_flapping"] is False
    state = state_row(pg_db, device)
    assert (state["hard_status"], state["status_since"], state["last_sample_at"]) == \
        ("degraded", confirmed_at, confirmed_at)

    [entry] = outbox(pg_db)
    payload = entry["payload"]
    assert "transitions" not in payload and "next_index" not in payload  # a single transition: legacy shape
    assert (payload["old_status"], payload["new_status"]) == ("up", "degraded")
    assert payload["reason"] == ("remote sensor edge-1: Latency 250.000 ms exceeds 100 ms by 150.000 ms; "
                                 "3 of the last 5 checks over limits or unanswered")
    assert payload["flapping"] is False and payload["is_flapping"] is False
    assert payload["device_id"] == str(device) and payload["rtt_ms"] == 250.0

    pings = [rows for table, rows in recorders.clickhouse if table == "ping_metrics"]
    assert sum(len(rows) for rows in pings) == 5             # every sample still reaches ping_metrics


@pytest.mark.asyncio
async def test_every_fresh_sample_of_a_backlog_batch_is_a_check(pg_db):
    sensor = add_sensor(pg_db)
    device = add_device(pg_db, sensor, "up")
    t = recent(90)
    # Before: only the newest sample of a batch counted, so this batch (ending clean) stayed up.
    await ingest(pg_db, sensor, [slow(device, t), slow(device, t + timedelta(seconds=10)),
                                 slow(device, t + timedelta(seconds=20)), ok(device, t + timedelta(seconds=30))])
    assert device_row(pg_db, device)["status"] == "degraded"
    [entry] = outbox(pg_db)
    # the transition carries the timestamp of the check that confirmed it, not of the newest sample
    assert sensor_health_service._outbox_timestamp(entry["payload"]["timestamp"]) == t + timedelta(seconds=20)


@pytest.mark.asyncio
async def test_stale_samples_feed_ping_metrics_but_never_the_status(pg_db, recorders):
    sensor = add_sensor(pg_db)
    device = add_device(pg_db, sensor, "up", interval=60)
    # older than max(2 x 60 s, 60 s): backfill only
    result = await ingest(pg_db, sensor, [fail(device, recent(600)), fail(device, recent(500))])
    assert result == {"inserted": 2, "dropped": 0, "duplicate": False}
    assert device_row(pg_db, device)["status"] == "up"
    assert state_row(pg_db, device) is None and outbox(pg_db) == []


@pytest.mark.asyncio
async def test_sensor_down_needs_its_own_confirmation_count(pg_db):
    set_policy(pg_db, {"sensor_down_confirm_checks": 2, "down_confirm_checks": 5})
    sensor = add_sensor(pg_db, "edge-2")
    device = add_device(pg_db, sensor, "up")
    t = recent(60)
    await ingest(pg_db, sensor, [fail(device, t)])
    assert device_row(pg_db, device)["status"] == "up" and state_row(pg_db, device)["fail_streak"] == 1
    await ingest(pg_db, sensor, [fail(device, t + timedelta(seconds=10))])
    assert device_row(pg_db, device)["status"] == "down"
    [entry] = outbox(pg_db)
    assert entry["payload"]["reason"] == "remote sensor edge-2: No response for 2 consecutive checks"


# ------------------------------------------------------------------ last response (last_seen / last_rtt_ms)

@pytest.mark.asyncio
async def test_unanswered_checks_never_move_the_last_response(pg_db):
    """The central writer's rule ('Last response' is the newest answered check): last_seen moves only
    for a reply; last_rtt_ms takes a reply's RTT, 0 once Down is confirmed, and is otherwise kept."""
    set_policy(pg_db, {"sensor_down_confirm_checks": 3})
    sensor = add_sensor(pg_db)
    device = add_device(pg_db, sensor, "up")
    t = recent(60)
    await ingest(pg_db, sensor, [sample(device, t, rtt=12.5)])
    assert (device_row(pg_db, device)["last_seen"], device_row(pg_db, device)["last_rtt_ms"]) == (t, 12.5)

    await ingest(pg_db, sensor, [fail(device, t + timedelta(seconds=10))])
    row = device_row(pg_db, device)
    # one lost check: still Up, and neither a fresh "response" nor an Up device at 0 ms
    assert (row["status"], row["last_seen"], row["last_rtt_ms"]) == ("up", t, 12.5)

    await ingest(pg_db, sensor, [fail(device, t + timedelta(seconds=20)), fail(device, t + timedelta(seconds=30))])
    row = device_row(pg_db, device)
    assert (row["status"], row["last_seen"], row["last_rtt_ms"]) == ("down", t, 0.0)
    await ingest(pg_db, sensor, [fail(device, t + timedelta(seconds=40))])
    assert (device_row(pg_db, device)["last_seen"], device_row(pg_db, device)["last_rtt_ms"]) == (t, 0.0)

    back = t + timedelta(seconds=50)
    await ingest(pg_db, sensor, [sample(device, back, rtt=7.0)])
    row = device_row(pg_db, device)
    assert (row["status"], row["last_seen"], row["last_rtt_ms"]) == ("up", back, 7.0)


@pytest.mark.asyncio
async def test_a_down_device_keeps_its_last_response_while_it_stays_down(pg_db):
    sensor = add_sensor(pg_db)
    device = add_device(pg_db, sensor, "down")
    answered = datetime.now(UTC) - timedelta(hours=1)
    pg_db.execute("UPDATE devices SET last_seen = $2, last_rtt_ms = 12.0, status_changed_at = $2 WHERE id = $1",
                  device, answered)
    t = recent(30)
    await ingest(pg_db, sensor, [fail(device, t), fail(device, t + timedelta(seconds=10))])
    row = device_row(pg_db, device)
    assert (row["status"], row["last_seen"], row["last_rtt_ms"]) == ("down", answered, 0.0)


@pytest.mark.asyncio
async def test_a_batch_that_answers_then_confirms_down_ends_at_zero_ms(pg_db):
    sensor = add_sensor(pg_db)
    device = add_device(pg_db, sensor, "up")
    t = recent(30)
    # default policy: one sensor failure confirms Down; the RTT follows the newest accepted check
    await ingest(pg_db, sensor, [sample(device, t, rtt=5.0), fail(device, t + timedelta(seconds=10))])
    row = device_row(pg_db, device)
    assert (row["status"], row["last_seen"], row["last_rtt_ms"]) == ("down", t, 0.0)

    # a late reply the machine ignores (older than the state) moves nothing either
    await ingest(pg_db, sensor, [sample(device, t + timedelta(seconds=5), rtt=3.0)])
    row = device_row(pg_db, device)
    assert (row["status"], row["last_seen"], row["last_rtt_ms"]) == ("down", t, 0.0)


# ------------------------------------------------------------------ vantages, ordering, idempotency

@pytest.mark.asyncio
async def test_samples_from_a_vantage_that_does_not_own_the_device_never_touch_its_state(pg_db, recorders):
    first, second = add_sensor(pg_db), add_sensor(pg_db)
    device = add_device(pg_db, first, "up")
    assign(pg_db, second, device)
    owner = owner_of(pg_db, device)
    vantage = second if owner == first else first
    t = recent(60)

    for offset in range(4):   # the vantage sees a hard outage the owner does not
        await ingest(pg_db, vantage, [fail(device, t + timedelta(seconds=10 * offset))])
    assert device_row(pg_db, device)["status"] == "up"
    assert state_row(pg_db, device) is None and outbox(pg_db) == []
    vantage_rows = [row for table, rows in recorders.clickhouse if table == "ping_metrics" for row in rows]
    assert len(vantage_rows) == 4 and {row["poller_id"] for row in vantage_rows} == {str(vantage)}

    await ingest(pg_db, owner, [ok(device, t + timedelta(seconds=45))])
    assert state_row(pg_db, device)["owner"] == str(owner)
    await ingest(pg_db, vantage, [fail(device, t + timedelta(seconds=50))])
    assert state_row(pg_db, device)["fail_streak"] == 0      # the owner's counters are not reset or advanced


@pytest.mark.asyncio
async def test_duplicate_and_out_of_order_samples_are_ignored(pg_db):
    set_policy(pg_db, {"sensor_down_confirm_checks": 3})
    sensor = add_sensor(pg_db)
    device = add_device(pg_db, sensor, "up")
    t = recent(60)

    await ingest(pg_db, sensor, [fail(device, t), fail(device, t)])            # a duplicate inside one batch
    assert state_row(pg_db, device)["fail_streak"] == 1
    await ingest(pg_db, sensor, [fail(device, t)])                             # re-sent in a later batch
    await ingest(pg_db, sensor, [fail(device, t - timedelta(seconds=5))])      # older than the state
    state = state_row(pg_db, device)
    assert state["fail_streak"] == 1 and state["last_sample_at"] == t
    assert device_row(pg_db, device)["status"] == "up"

    batch = [fail(device, t + timedelta(seconds=10))]
    first = await ingest(pg_db, sensor, batch, key="replayed-batch-0001")
    assert await ingest(pg_db, sensor, batch, key="replayed-batch-0001") == {**first, "duplicate": True}
    assert state_row(pg_db, device)["fail_streak"] == 2       # the ledger stops an exact replay
    assert device_row(pg_db, device)["status"] == "up"
    await ingest(pg_db, sensor, [fail(device, t + timedelta(seconds=20))])
    assert device_row(pg_db, device)["status"] == "down"


# ------------------------------------------------------------------ outbox: several transitions, cursor replay

@pytest.mark.asyncio
async def test_multi_transition_batch_replays_from_its_cursor_after_a_failure(pg_db, recorders, monkeypatch):
    set_policy(pg_db, LEGACY)
    sensor = add_sensor(pg_db, "edge-3")
    device = add_device(pg_db, sensor, "up")
    t = recent(60)
    await ingest(pg_db, sensor, [slow(device, t), ok(device, t + timedelta(seconds=10)),
                                 slow(device, t + timedelta(seconds=20))])

    [entry] = outbox(pg_db)
    payload = entry["payload"]
    steps = [(item["old_status"], item["new_status"]) for item in payload["transitions"]]
    assert steps == [("up", "degraded"), ("degraded", "up"), ("up", "degraded")]
    assert payload["next_index"] == 0
    # Legacy top-level fields describe the LAST transition (what an older dispatcher delivers).
    assert (payload["old_status"], payload["new_status"]) == ("up", "degraded")
    assert payload["timestamp"] == payload["transitions"][-1]["timestamp"]
    assert device_row(pg_db, device)["status"] == "degraded"

    # The alert engine commits its own rows; model that with an audit row per delivered transition,
    # and fail the second transition once, after it wrote its rows.
    calls = {"n": 0}

    async def evaluate(event, db):
        calls["n"] += 1
        await db.execute(text("INSERT INTO audit_logs (action, resource_type, resource_id) "
                              "VALUES ('test.evaluate', 'transition', :rid)"),
                         {"rid": f"{event.old_status}->{event.new_status}"})
        if calls["n"] == 2:
            raise RuntimeError("alert engine unavailable")
        await db.commit()
        recorders.evaluated.append(event)
        return {}

    monkeypatch.setattr(alert_engine, "evaluate_status_change", evaluate)

    first = await dispatch(pg_db)
    assert first == {"processed": 1, "failed": 1}
    [entry] = outbox(pg_db)
    assert entry["payload"]["next_index"] == 1 and entry["processed_at"] is None
    assert entry["attempts"] == 1 and "alert engine unavailable" in entry["last_error"]
    assert pg_db.query("SELECT resource_id FROM audit_logs WHERE action = 'test.evaluate'") == [("up->degraded",)]

    pg_db.execute("UPDATE sensor_transition_outbox SET next_attempt_at = now() - interval '1 second'")
    assert await dispatch(pg_db) == {"processed": 2, "failed": 0}
    [entry] = outbox(pg_db)
    assert entry["payload"]["next_index"] == 3 and entry["processed_at"] is not None
    delivered = pg_db.query("SELECT resource_id FROM audit_logs WHERE action = 'test.evaluate' ORDER BY created_at")
    assert sorted(r[0] for r in delivered) == ["degraded->up", "up->degraded", "up->degraded"]
    assert [(e.old_status, e.new_status) for e in recorders.evaluated] == steps
    assert [(p["old_status"], p["new_status"]) for p in recorders.published] == steps   # each exactly once
    assert [p["reason"] for p in recorders.published][1] == (
        "remote sensor edge-3: Within recovery limits (100 ms, 10%) for 1 consecutive checks")
    assert await dispatch(pg_db) == {"processed": 0, "failed": 0}


@pytest.mark.asyncio
async def test_the_real_alert_engine_commits_each_transition_with_its_cursor(pg_db, recorders, monkeypatch):
    monkeypatch.setattr(alert_engine, "evaluate_status_change", REAL_EVALUATE)
    set_policy(pg_db, LEGACY)
    sensor = add_sensor(pg_db, "edge-6")
    device = add_device(pg_db, sensor, "down")
    pg_db.execute("INSERT INTO alerts (device_id, status, severity, message, metadata) "
                  "VALUES ($1, 'active', 'critical', 'down', '{\"new_status\": \"down\"}')", device)
    t = recent(30)
    await ingest(pg_db, sensor, [ok(device, t), slow(device, t + timedelta(seconds=10))])
    payload = outbox(pg_db)[0]["payload"]
    assert [(i["old_status"], i["new_status"], i["reason"]) for i in payload["transitions"]] == [
        ("down", "up", "remote sensor edge-6: Responding again"),
        ("up", "degraded", "remote sensor edge-6: Latency 250.000 ms exceeds 100 ms by 150.000 ms; "
                           "1 of the last 1 checks over limits or unanswered"),
    ]

    assert await dispatch(pg_db) == {"processed": 2, "failed": 0}
    [entry] = outbox(pg_db)
    assert entry["processed_at"] is not None and entry["payload"]["next_index"] == 2
    assert pg_db.scalar("SELECT status FROM alerts WHERE device_id = $1 AND message = 'down'", device) == "resolved"


@pytest.mark.asyncio
async def test_a_legacy_single_transition_row_still_dispatches(pg_db, recorders):
    sensor = add_sensor(pg_db)
    device = add_device(pg_db, sensor, "degraded")
    ts = recent(30)
    legacy = {"device_id": str(device), "hostname": "legacy", "ip_address": "10.0.0.9", "device_type": "router",
              "group_id": None, "location": None, "old_status": "degraded", "new_status": "up",
              "timestamp": str(ts), "rtt_ms": 4.0, "packet_loss": 0.0, "reason": "remote sensor old"}
    pg_db.execute("INSERT INTO sensor_transition_outbox (sensor_id, endpoint, idempotency_key, transition_type, "
                  "entity_id, payload) VALUES ($1, 'ping', 'legacy-row-0001', 'device', $2, $3)",
                  sensor, device, legacy)
    assert await dispatch(pg_db) == {"processed": 1, "failed": 0}
    [entry] = outbox(pg_db)
    assert entry["processed_at"] is not None and "next_index" not in entry["payload"]
    [event] = recorders.evaluated
    assert (event.old_status, event.new_status, event.flapping, event.is_flapping) == ("degraded", "up", False, False)
    [logged] = [rows for table, rows in recorders.clickhouse if table == "device_status_log"]
    assert logged[0]["timestamp"] == ts
    [published] = recorders.published
    assert published["new_status"] == "up"    # the publisher streams is_flapping=false for old rows


@pytest.mark.asyncio
async def test_published_stream_message_matches_the_poller_shape(monkeypatch):
    sent = []

    class FakeRedis:
        async def publish(self, channel, message):
            sent.append((channel, json.loads(message)))

    monkeypatch.setattr(sensor_api, "_status_redis", FakeRedis())
    monkeypatch.setattr(sensor_api, "_status_redis_down_until", 0.0)
    ts = datetime(2026, 10, 14, 12, 0, 1, 500000, tzinfo=UTC)
    await REAL_PUBLISH({"device_id": uuid.UUID(int=7), "old_status": "up", "new_status": "down",
                        "reason": "remote sensor x: No response for 1 consecutive checks", "timestamp": ts,
                        "is_flapping": True, "hostname": "not streamed"})
    assert sent == [("zenplus:status_change", {
        "device_id": str(uuid.UUID(int=7)), "old_status": "up", "new_status": "down",
        "reason": "remote sensor x: No response for 1 consecutive checks",
        "timestamp": "2026-10-14T12:00:01.5Z", "is_flapping": True})]

    class BrokenRedis:
        async def publish(self, channel, message):
            raise ConnectionError("redis is down")

    monkeypatch.setattr(sensor_api, "_status_redis", BrokenRedis())
    await REAL_PUBLISH({"device_id": "d", "old_status": "up", "new_status": "down", "timestamp": ts})
    assert sensor_api._status_redis_down_until > 0   # best effort: never raises, pauses publishing


# ------------------------------------------------------------------ flapping, maintenance, offline

@pytest.mark.asyncio
async def test_flapping_opens_an_episode_and_sensor_offline_cleans_up(pg_db, recorders):
    set_policy(pg_db, {**LEGACY, "flap_detection_enabled": True, "flap_start_changes": 3})
    sensor = add_sensor(pg_db, "edge-4")
    device = add_device(pg_db, sensor, "up")
    t = recent(80)
    await ingest(pg_db, sensor, [slow(device, t), ok(device, t + timedelta(seconds=10)),
                                 slow(device, t + timedelta(seconds=20))])

    row = device_row(pg_db, device)
    flap_at = t + timedelta(seconds=20)
    assert (row["status"], row["is_flapping"], row["flap_kind"], row["flapping_since"]) == \
        ("degraded", True, "performance", flap_at)
    [episode] = pg_db.query("SELECT started_at, ended_at, kind, start_changes, window_minutes, status_at_start, "
                            "start_notice FROM device_flap_episodes WHERE device_id = $1", device)
    assert episode == (flap_at, None, "performance", 3, 30, "degraded", "pending")
    transitions = outbox(pg_db)[0]["payload"]["transitions"]
    assert [item["flapping"] for item in transitions] == [False, False, True]
    assert [item["is_flapping"] for item in transitions] == [False, False, True]

    # The flags reach the alert engine (which holds flapping triggers) and the live stream.
    assert await dispatch(pg_db) == {"processed": 3, "failed": 0}
    assert [(e.flapping, e.is_flapping) for e in recorders.evaluated] == [(False, False), (False, False), (True, True)]
    assert [p["is_flapping"] for p in recorders.published] == [False, False, True]

    pg_db.execute("INSERT INTO alerts (device_id, status, severity, message, metadata) "
                  "VALUES ($1, 'active', 'warning', 'degraded', '{\"new_status\": \"degraded\"}')", device)
    pg_db.execute("UPDATE sensors SET last_heartbeat_at = now() - interval '1 hour' WHERE id = $1", sensor)
    engine, session = await session_for(pg_db)
    try:
        async with session:
            swept = await sensor_health_service.sweep_sensor_health(session)
    finally:
        await engine.dispose()
    assert swept["offline"] == 1 and swept["alerts_suppressed"] == 1

    row = device_row(pg_db, device)
    assert (row["status"], row["is_flapping"], row["flapping_since"], row["flap_kind"]) == \
        ("unknown", False, None, None)
    assert row["status_changed_at"] > datetime.now(UTC) - timedelta(minutes=1)
    assert state_row(pg_db, device) is None
    assert pg_db.query("SELECT end_reason, status_at_end, ended_at IS NOT NULL FROM device_flap_episodes "
                       "WHERE device_id = $1", device) == [("owner_change", "unknown", True)]
    assert pg_db.scalar("SELECT status FROM alerts WHERE device_id = $1", device) == "resolved"

    # Back online: the device is reseeded from 'unknown' and its first answer is a fresh baseline.
    pg_db.execute("UPDATE sensors SET status = 'online', last_heartbeat_at = now() WHERE id = $1", sensor)
    await ingest(pg_db, sensor, [ok(device, recent(5))])
    assert device_row(pg_db, device)["status"] == "up"
    assert outbox(pg_db)[-1]["payload"]["reason"] == "remote sensor edge-4: Device responding normally"


@pytest.mark.asyncio
async def test_maintenance_entry_is_logged_and_streamed_but_never_evaluated(pg_db, recorders):
    sensor = add_sensor(pg_db, "edge-5")
    device = add_device(pg_db, sensor, "up")
    pg_db.execute("INSERT INTO device_maintenance (scope_type, scope_device_id, starts_at, ends_at) "
                  "VALUES ('device', $1, now() - interval '5 minutes', now() + interval '1 hour')", device)
    at = recent(20)
    await ingest(pg_db, sensor, [ok(device, at)])
    row = device_row(pg_db, device)
    assert row["status"] == "maintenance" and row["status_changed_at"] == at
    assert state_row(pg_db, device)["in_maintenance"] is True
    payload = outbox(pg_db)[0]["payload"]
    assert (payload["old_status"], payload["new_status"], payload["reason"]) == \
        ("up", "maintenance", "remote sensor edge-5: Maintenance window active")

    assert await dispatch(pg_db) == {"processed": 1, "failed": 0}
    assert recorders.evaluated == []
    assert [rows[0]["new_status"] for table, rows in recorders.clickhouse if table == "device_status_log"] == \
        ["maintenance"]
    assert [p["new_status"] for p in recorders.published] == ["maintenance"]


def add_window(db: PgDatabase, device, starts_at: datetime, ends_at: datetime) -> None:
    db.execute("INSERT INTO device_maintenance (scope_type, scope_device_id, starts_at, ends_at) "
               "VALUES ('device', $1, $2, $3)", device, starts_at, ends_at)


def device_transitions(db: PgDatabase, device) -> list[tuple[str, str]]:
    steps = []
    for entry in outbox(db, device):
        payload = entry["payload"]
        steps += [(item["old_status"], item["new_status"]) for item in payload.get("transitions", [payload])]
    return steps


@pytest.mark.asyncio
async def test_a_backlog_that_crosses_a_window_end_decides_maintenance_per_sample(pg_db, recorders):
    """A failure taken inside the window and uploaded after it ended stays inside the window: the
    overlay ends with the post-window answer (maintenance -> up), never maintenance -> down -> up."""
    sensor = add_sensor(pg_db, "edge-7")
    device = add_device(pg_db, sensor, "up", interval=60)
    now = datetime.now(UTC)
    add_window(pg_db, device, now - timedelta(minutes=10), now - timedelta(seconds=30))

    await ingest(pg_db, sensor, [ok(device, now - timedelta(seconds=100))])     # taken inside the window
    assert device_row(pg_db, device)["status"] == "maintenance"
    await ingest(pg_db, sensor, [fail(device, now - timedelta(seconds=60)),       # inside: the device rebooting
                                 ok(device, now - timedelta(seconds=10))])        # after the window
    row = device_row(pg_db, device)
    assert (row["status"], row["status_changed_at"]) == ("up", now - timedelta(seconds=10))
    assert device_transitions(pg_db, device) == [("up", "maintenance"), ("maintenance", "up")]
    assert outbox(pg_db, device)[-1]["payload"]["reason"] == \
        "remote sensor edge-7: Maintenance ended; device responding normally"


@pytest.mark.asyncio
async def test_a_device_still_failing_after_the_window_leaves_it_and_is_evaluated(pg_db, recorders):
    sensor = add_sensor(pg_db, "edge-8")
    device = add_device(pg_db, sensor, "up", interval=60)
    now = datetime.now(UTC)
    add_window(pg_db, device, now - timedelta(minutes=10), now - timedelta(seconds=30))
    await ingest(pg_db, sensor, [ok(device, now - timedelta(seconds=100))])
    await ingest(pg_db, sensor, [fail(device, now - timedelta(seconds=60)),
                                 fail(device, now - timedelta(seconds=10))])
    assert device_row(pg_db, device)["status"] == "down"
    assert device_transitions(pg_db, device) == [("up", "maintenance"), ("maintenance", "down")]
    payload = outbox(pg_db, device)[-1]["payload"]
    assert payload["reason"] == "remote sensor edge-8: Maintenance ended; no response for 2 consecutive checks"
    assert sensor_health_service._outbox_timestamp(payload["timestamp"]) == now - timedelta(seconds=10)

    assert await dispatch(pg_db) == {"processed": 2, "failed": 0}
    assert [(e.old_status, e.new_status) for e in recorders.evaluated] == [("maintenance", "down")]
    engine, session = await session_for(pg_db)
    try:
        async with session:   # the alert engine's own check does not suppress the exit
            assert await alert_engine._device_in_maintenance(session, str(device)) is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_sensor_clock_ahead_never_leaves_an_active_window_early(pg_db, recorders):
    """Sample times are clamped to the server clock: a sample stamped after the window's end by a
    sensor running ahead, uploaded while the window is still active, keeps the overlay (otherwise the
    dispatcher would suppress that exit and the device would never page after the window)."""
    sensor = add_sensor(pg_db)
    device = add_device(pg_db, sensor, "up", interval=60)
    now = datetime.now(UTC)
    add_window(pg_db, device, now - timedelta(minutes=5), now + timedelta(seconds=20))
    await ingest(pg_db, sensor, [fail(device, now + timedelta(seconds=40))])
    assert device_row(pg_db, device)["status"] == "maintenance"
    state = state_row(pg_db, device)
    assert (state["hard_status"], state["in_maintenance"]) == ("down", True)
    assert device_transitions(pg_db, device) == [("up", "maintenance")]


def test_sample_maintenance_is_decided_at_the_clamped_sample_time():
    raw = [["2026-10-15T01:00:00+00:00", "2026-10-15T02:00:00+00:00"],
           ["2026-10-15T05:30:10.5+03:00", "2026-10-15T05:30:20+03:00"]]   # jsonb renders the session zone
    windows = sensor_api._maintenance_windows({"maintenance_windows": raw})
    assert windows == sensor_api._maintenance_windows({"maintenance_windows": json.dumps(raw)})
    assert windows[1] == (datetime(2026, 10, 15, 2, 30, 10, 500000, tzinfo=UTC),
                          datetime(2026, 10, 15, 2, 30, 20, tzinfo=UTC))
    assert sensor_api._maintenance_windows({"maintenance_windows": None}) == []

    def inside(h, m, s, now=(2, 0, 5)):
        return sensor_api._sample_in_maintenance(
            windows, datetime(2026, 10, 15, h, m, s, tzinfo=UTC), datetime(2026, 10, 15, *now, tzinfo=UTC))

    assert inside(1, 59, 50) is True
    assert inside(2, 0, 0) is True                  # bounds inclusive, like the dispatcher's check
    assert inside(2, 0, 1) is False                 # taken after the window: no overlay
    assert inside(2, 0, 50, now=(1, 59, 58)) is True  # sensor clock ahead: decided at the server's 01:59:58
    assert inside(2, 30, 15, now=(2, 30, 30)) is True
    assert inside(2, 30, 10, now=(2, 30, 30)) is False


@pytest.mark.asyncio
async def test_an_owner_change_reseeds_silently_and_closes_the_previous_owners_episode(pg_db):
    sensor = add_sensor(pg_db)
    device = add_device(pg_db, sensor, "down")
    since = datetime.now(UTC) - timedelta(hours=2)
    pg_db.execute("UPDATE devices SET status_changed_at = $2, is_flapping = true, flap_kind = 'reachability', "
                  "flapping_since = $2 WHERE id = $1", device, since)
    pg_db.execute("INSERT INTO device_status_state (device_id, owner, hard_status, fail_streak, reach_flapping, "
                  "flapping_since) VALUES ($1, 'central', 'down', 9, true, $2)", device, since)
    pg_db.execute("INSERT INTO device_flap_episodes (device_id, started_at, kind) VALUES ($1, $2, 'reachability')",
                  device, since)

    at = recent(10)
    await ingest(pg_db, sensor, [fail(device, at)])
    row = device_row(pg_db, device)
    assert (row["status"], row["is_flapping"], row["flap_kind"]) == ("down", False, None)
    assert row["status_changed_at"] == since                 # still down: no change, no new timestamp
    state = state_row(pg_db, device)
    assert (state["owner"], state["hard_status"], state["status_since"]) == (str(sensor), "down", since)
    assert pg_db.query("SELECT end_reason, status_at_end, ended_at FROM device_flap_episodes WHERE device_id = $1",
                       device) == [("owner_change", "down", at)]
    assert outbox(pg_db) == []                               # a reseed never emits events


@pytest.mark.asyncio
async def test_an_overlay_left_by_another_writer_clears_on_the_next_accepted_check(pg_db):
    sensor = add_sensor(pg_db)
    device = add_device(pg_db, sensor, "up")
    t = recent(60)
    await ingest(pg_db, sensor, [ok(device, t)])
    # A window created and deleted between two uploads: its writer showed 'maintenance' and left a
    # ping-enabled device to its poller. The machine never entered maintenance, so no transition.
    earlier = datetime.now(UTC) - timedelta(minutes=5)
    pg_db.execute("UPDATE devices SET status = 'maintenance', status_changed_at = $2 WHERE id = $1",
                  device, earlier)

    await ingest(pg_db, sensor, [ok(device, t)])               # ignored (not newer than the state)
    assert device_row(pg_db, device)["status"] == "maintenance"
    at = t + timedelta(seconds=10)
    await ingest(pg_db, sensor, [ok(device, at)])
    row = device_row(pg_db, device)
    assert (row["status"], row["status_changed_at"]) == ("up", at)
    assert state_row(pg_db, device)["hard_status"] == "up" and outbox(pg_db) == []


# ------------------------------------------------------------------ isolation

@pytest.mark.asyncio
async def test_one_device_that_cannot_be_written_does_not_poison_the_batch(pg_db, monkeypatch):
    sensor = add_sensor(pg_db)
    good, bad = add_device(pg_db, sensor, "up"), add_device(pg_db, sensor, "up")
    real_write = sensor_api._write_device_status

    async def write(plans, db):
        await real_write(plans, db)
        if any(plan.device_id == str(bad) for plan in plans):
            await db.execute(text("SELECT 1 / 0"))   # a database error inside the device's savepoint

    monkeypatch.setattr(sensor_api, "_write_device_status", write)
    t = recent(30)
    result = await ingest(pg_db, sensor, [fail(good, t), fail(bad, t)])
    assert result == {"inserted": 2, "dropped": 0, "duplicate": False}
    assert device_row(pg_db, good)["status"] == "down" and state_row(pg_db, good)["hard_status"] == "down"
    assert device_row(pg_db, bad)["status"] == "up" and state_row(pg_db, bad) is None
    assert [entry["payload"]["device_id"] for entry in outbox(pg_db)] == [str(good)]
    assert pg_db.scalar("SELECT completed_at IS NOT NULL FROM sensor_ingest_ledger") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["ingest", "sensor_offline"])
async def test_a_flap_notice_claim_and_a_status_writer_never_deadlock(pg_db, writer):
    """The flap notice sweep keeps its claimed episode locked while it inserts the notice row, whose
    foreign key takes KEY SHARE on the devices row. The writers lock devices FOR NO KEY UPDATE, which
    KEY SHARE passes, so the claim commits and the writer then closes the episode. (FOR UPDATE would
    hold the notice insert behind the writer while the writer waits for the episode: a deadlock that
    cost the writer its status update.)"""
    set_policy(pg_db, {**LEGACY, "flap_detection_enabled": True, "flap_start_changes": 3})
    sensor = add_sensor(pg_db)
    device = add_device(pg_db, sensor, "up")
    t = recent(80)
    await ingest(pg_db, sensor, [slow(device, t), ok(device, t + timedelta(seconds=10)),
                                 slow(device, t + timedelta(seconds=20))])
    [(episode,)] = pg_db.query("SELECT id FROM device_flap_episodes WHERE device_id = $1 AND ended_at IS NULL",
                               device)
    if writer == "ingest":
        set_policy(pg_db, LEGACY)        # flap detection off: the next check ends the episode
        end_reason = "disabled"
    else:
        pg_db.execute("UPDATE sensors SET last_heartbeat_at = now() - interval '1 hour' WHERE id = $1", sensor)
        end_reason = "owner_change"

    engine = create_async_engine(pg_db.url, poolclass=NullPool)
    try:
        async with engine.connect() as claim:
            await claim.execute(text("SELECT id FROM device_flap_episodes WHERE id = :id FOR UPDATE"),
                                {"id": episode})
            task = asyncio.create_task(
                ingest(pg_db, sensor, [ok(device, t + timedelta(seconds=30))]) if writer == "ingest"
                else sweep(pg_db))
            blocked = 0
            for _ in range(200):
                blocked = (await claim.execute(text("SELECT count(*) FROM pg_locks WHERE NOT granted"))).scalar()
                if blocked or task.done():
                    break
                await asyncio.sleep(0.05)
            assert blocked                       # the writer waits for the claimed episode
            await claim.execute(text("SET LOCAL lock_timeout = '5s'"))
            await claim.execute(text("INSERT INTO alerts (device_id, status, severity, message) "
                                     "VALUES (:id, 'resolved', 'info', 'flap notice')"), {"id": device})
            await claim.commit()
        await asyncio.wait_for(task, 15)
    finally:
        await engine.dispose()
    assert pg_db.query("SELECT end_reason FROM device_flap_episodes WHERE id = $1", episode) == [(end_reason,)]


@pytest.mark.asyncio
async def test_a_device_whose_evaluation_raises_is_skipped(pg_db, monkeypatch):
    sensor = add_sensor(pg_db)
    good, bad = add_device(pg_db, sensor, "up"), add_device(pg_db, sensor, "up")
    real_plan = sensor_api._plan_device_status

    def plan(info, *args, **kwargs):
        if info["device_id"] == str(bad):
            raise ValueError("corrupt state")
        return real_plan(info, *args, **kwargs)

    monkeypatch.setattr(sensor_api, "_plan_device_status", plan)
    t = recent(30)
    await ingest(pg_db, sensor, [fail(good, t), fail(bad, t)])
    assert device_row(pg_db, good)["status"] == "down"
    assert device_row(pg_db, bad)["status"] == "up" and state_row(pg_db, bad) is None


@pytest.mark.asyncio
async def test_an_offline_sensor_still_cannot_upload(pg_db):
    sensor = add_sensor(pg_db)
    device = add_device(pg_db, sensor, "up")
    pg_db.execute("UPDATE sensors SET status = 'offline' WHERE id = $1", sensor)
    with pytest.raises(HTTPException) as excinfo:
        await ingest(pg_db, sensor, [fail(device, recent(5))])
    assert excinfo.value.status_code == 409
    assert state_row(pg_db, device) is None


def test_outbox_payload_shapes():
    one = {"device_id": "d", "old_status": "up", "new_status": "down"}
    assert sensor_api._device_outbox_payload([one]) is one
    assert sensor_api._device_outbox_events(one) == ([one], 0)
    two = {"device_id": "d", "old_status": "down", "new_status": "up"}
    payload = sensor_api._device_outbox_payload([one, two])
    assert payload == {**two, "transitions": [one, two], "next_index": 0}
    assert sensor_api._device_outbox_events({**payload, "next_index": 1}) == ([one, two], 1)
    assert sensor_api._device_outbox_events({**payload, "next_index": 9})[1] == 2
    assert sensor_api._device_outbox_events({**payload, "next_index": "x"})[1] == 0
    assert dsm.format_ts(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_a_batch_in_flight_after_ping_is_switched_off_never_recreates_the_state(pg_db, recorders):
    """device_polling_owner ignores ping_enabled; the device update has already released the device,
    so a late batch only feeds ping_metrics."""
    sensor = add_sensor(pg_db)
    device = add_device(pg_db, sensor, "up")
    pg_db.execute("UPDATE devices SET ping_enabled = false WHERE id = $1", device)
    t = recent(30)

    await ingest(pg_db, sensor, [slow(device, t), slow(device, t + timedelta(seconds=5)),
                                 slow(device, t + timedelta(seconds=10))])

    assert state_row(pg_db, device) is None and outbox(pg_db) == []
    row = device_row(pg_db, device)
    assert row["status"] == "up" and row["is_flapping"] is False and row["last_seen"] is None
    assert sum(len(rows) for table, rows in recorders.clickhouse if table == "ping_metrics") == 3
