"""Device read APIs over confirmed status and flapping (migration 161), against a disposable Postgres.

* ``GET /devices`` serialises ``is_flapping``, ``flapping_since``, ``flap_kind`` and ``status_changed_at``, and its
  ``flapping`` filter is an overlay on the status (combinable with ``status``, honoured by the facets).
* ``GET /devices/summary`` counts flapping devices beside, not instead of, their status bucket.
* ``GET /devices/{id}/status-explanation`` explains from the effective global policy and the owning writer's state
  for central and sensor owners: pending progress, the streak that applies, the flapping block, and a silent seed when
  the stored state belongs to another owner or was overtaken.
* ``GET /devices/{id}/flap-episodes`` lists episodes overlapping a period, newest first, scope-checked (404).
* The device_flapping report section's episode aggregate runs on the real schema; maintenance create/delete stamp
  ``status_changed_at`` when they change a device's status.

ClickHouse is a fake that answers nothing; nothing here reaches an installed database.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db, pg_db_module  # noqa: F401  (fixtures)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
SENSOR_ID = uuid.uuid4()
OTHER_SENSOR = str(uuid.uuid4())
ID = {name: uuid.uuid4() for name in ("flap", "pending", "sensor", "reseed", "hidden", "off")}
POLICY = {"up_confirm_checks": 2, "sensor_down_confirm_checks": 2, "down_confirm_checks": 3, "degraded_rtt_ms": 150}


def ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


@pytest.fixture(scope="module")
def world(pg_db_module):
    db = pg_db_module
    devices = [
        # name, hostname, ip, tags, status, ping, is_flapping, flapping_since, flap_kind, status_changed_at
        ("flap", "core-flap", "10.9.0.1", ["team-a"], "down", True, True, ago(minutes=10), "reachability",
         ago(minutes=2)),
        ("pending", "edge-pending", "10.9.0.2", ["team-a"], "up", True, False, None, None, ago(hours=3)),
        ("sensor", "branch-sensor", "10.9.0.3", ["team-a"], "up", True, False, None, None, ago(days=1)),
        ("reseed", "wan-reseed", "10.9.0.4", ["team-a"], "degraded", True, False, None, None, ago(hours=1)),
        ("hidden", "lab-hidden", "10.9.0.5", ["team-b"], "up", True, True, ago(minutes=5), "performance", None),
        ("off", "printer-off", "10.9.0.6", ["team-a"], "unknown", False, False, None, None, None),
    ]
    for name, host, ip, tags, status, ping, flapping, since, kind, changed in devices:
        db.execute(
            "INSERT INTO devices (id, hostname, ip_address, tags, status, ping_enabled, ping_interval, is_flapping,"
            " flapping_since, flap_kind, status_changed_at, vendor)"
            " VALUES ($1, $2, $3::inet, $4::jsonb, $5, $6, 60, $7, $8, $9, $10, 'Acme')",
            ID[name], host, ip, tags, status, ping, flapping, since, kind, changed)
    db.execute("INSERT INTO sensors (id, name, status) VALUES ($1, 'branch-site', 'online')", SENSOR_ID)
    db.execute("INSERT INTO sensor_assignments (sensor_id, target_type, target_id) VALUES ($1, 'device', $2)",
               SENSOR_ID, ID["sensor"])
    db.execute("INSERT INTO system_settings (key, value) VALUES ('monitoring', $1::jsonb)"
               " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", POLICY)

    def state(name, owner, hard, **kw):
        row = dict(fail_streak=0, ok_streak=0, clear_streak=0, window_bits=0, window_len=0, reach_changes=[],
                   perf_changes=[], reach_flapping=False, perf_flapping=False, flapping_since=None,
                   status_since=None, last_sample_at=ago(seconds=15))
        row.update(kw)
        db.execute(
            "INSERT INTO device_status_state (device_id, owner, hard_status, fail_streak, ok_streak, clear_streak,"
            " window_bits, window_len, reach_changes, perf_changes, reach_flapping, perf_flapping, flapping_since,"
            " status_since, last_sample_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)",
            ID[name], owner, hard, row["fail_streak"], row["ok_streak"], row["clear_streak"], row["window_bits"],
            row["window_len"], row["reach_changes"], row["perf_changes"], row["reach_flapping"],
            row["perf_flapping"], row["flapping_since"], row["status_since"], row["last_sample_at"])

    # Down, flapping on reachability, answered once of the two checks that confirm a recovery.
    state("flap", "central", "down", ok_streak=1, fail_streak=0, reach_flapping=True, flapping_since=ago(minutes=10),
          reach_changes=[ago(minutes=20), ago(minutes=15), ago(minutes=10), ago(minutes=2)],
          status_since=ago(minutes=2))
    # Up with two slow checks of the last three: Degraded is pending (2 of 3).
    state("pending", "central", "up", window_bits=0b101, window_len=3, ok_streak=1, status_since=ago(hours=3))
    # Sensor-owned, one failed sensor check of the two that confirm Down.
    state("sensor", str(SENSOR_ID), "up", fail_streak=1, window_bits=0b1, window_len=1, status_since=ago(days=1))
    # Left behind by another owner: the writer reseeds it, so the explanation must not trust it.
    state("reseed", OTHER_SENSOR, "degraded", fail_streak=2, clear_streak=2, status_since=ago(days=9))

    def episode(name, started, ended, kind, end_reason=None):
        db.execute("INSERT INTO device_flap_episodes (device_id, started_at, ended_at, kind, start_changes,"
                   " window_minutes, status_at_start, status_at_end, end_reason)"
                   " VALUES ($1, $2, $3, $4, 4, 30, 'up', $5, $6)",
                   ID[name], started, ended, kind, "up" if ended else None, end_reason)

    episode("flap", ago(days=3), ago(days=3) + timedelta(minutes=40), "both", "stable")
    episode("flap", ago(minutes=10), None, "reachability")
    episode("hidden", ago(minutes=5), None, "performance")
    return db


@pytest.fixture
def ch_calls(monkeypatch):
    """ClickHouse for the explanation's evidence: no samples, no degraded transitions."""
    from app.services import device_status_explanation as module
    calls: list[dict] = []

    class Client:
        def query(self, sql, parameters=None, **kw):
            calls.append(dict(parameters or {}))
            return SimpleNamespace(result_rows=[])

    monkeypatch.setattr(module, "get_clickhouse_client", lambda: Client())
    return calls


def team_a(db):
    return api_client(db, role="t_flap_team_a", permissions=["devices.view"], scope_tags=("team-a",))


def everyone(db):
    return api_client(db, role="t_flap_all", permissions=["devices.view"])


def hostnames(c, **params) -> set[str]:
    resp = c.get("/api/v1/devices", params={"limit": 200, **params})
    assert resp.status_code == 200, resp.text[:300]
    return {d["hostname"] for d in resp.json()["data"]}


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ── list, filter, summary ───────────────────────────────────────────────────

def test_list_serialises_the_flapping_overlay_and_status_change_time(world):
    with team_a(world) as c:
        items = {d["hostname"]: d for d in c.get("/api/v1/devices", params={"limit": 200}).json()["data"]}
        flap = items["core-flap"]
        assert flap["status"] == "down" and flap["is_flapping"] is True and flap["flap_kind"] == "reachability"
        assert parse(flap["flapping_since"]) == ago(minutes=10)
        assert parse(flap["status_changed_at"]) == ago(minutes=2)
        calm = items["edge-pending"]
        assert calm["is_flapping"] is False and calm["flap_kind"] is None and calm["flapping_since"] is None
        assert items["printer-off"]["status_changed_at"] is None
        detail = c.get(f"/api/v1/devices/{ID['flap']}").json()
        assert detail["is_flapping"] is True and detail["flap_kind"] == "reachability"


def test_flapping_filter_is_an_overlay_combinable_with_status(world):
    with team_a(world) as c:
        assert hostnames(c, flapping="true") == {"core-flap"}
        assert hostnames(c, flapping="1") == {"core-flap"}
        assert hostnames(c, status="down", flapping="true") == {"core-flap"}
        assert hostnames(c, status="up", flapping="true") == set()
        assert hostnames(c, flapping="false") == {"edge-pending", "branch-sensor", "wan-reseed", "printer-off"}
        # The tag scope still applies: team-b's flapping device stays invisible.
        body = c.get("/api/v1/devices", params={"flapping": "true", "facets": "true"}).json()
        assert body["meta"]["total"] == 1
        assert body["facets"]["vendor"]["items"] == [{"value": "Acme", "label": "Acme", "count": 1}]
    with everyone(world) as c:
        assert hostnames(c, flapping="true") == {"core-flap", "lab-hidden"}


def test_summary_counts_flapping_beside_the_status_buckets(world):
    with team_a(world) as c:
        body = c.get("/api/v1/devices/summary").json()
        assert body == {"total": 5, "up": 2, "down": 1, "degraded": 1, "unknown": 1, "maintenance": 0,
                        "flapping": 1}
    with everyone(world) as c:
        body = c.get("/api/v1/devices/summary").json()
        assert body["total"] == world.scalar("SELECT count(*) FROM devices")
        assert body["up"] == world.scalar("SELECT count(*) FROM devices WHERE status = 'up'")
        assert body["flapping"] == 2


# ── status explanation ──────────────────────────────────────────────────────

def explanation(c, name: str) -> dict:
    resp = c.get(f"/api/v1/devices/{ID[name]}/status-explanation")
    assert resp.status_code == 200, resp.text[:300]
    return resp.json()


def test_explanation_for_a_central_device_pending_recovery_while_flapping(world, ch_calls):
    with team_a(world) as c:
        body = explanation(c, "flap")
    assert body["mode"] == "ping" and body["source"] == "Controller" and body["status"] == "down"
    policy = body["policy"]
    assert policy["scope"] == "global"
    assert policy["up_confirm_checks"] == 2 and policy["degraded_rtt_ms"] == 150
    assert policy["degraded_rtt_clear_ms"] == 120 and policy["degraded_loss_clear_pct"] == 8
    assert body["thresholds"] == {"degraded_rtt_ms": 150, "degraded_loss_pct": 10, "degraded_rtt_clear_ms": 120,
                                  "degraded_loss_clear_pct": 8}
    assert body["confirmation"] == {"failed_checks": 3, "packets_per_check": 3, "interval_seconds": 60,
                                    "recovery_checks": 2}
    state = body["state"]
    assert state["hard_status"] == "down" and state["seeded"] is False and state["stale"] is False
    assert state["pending"] == {"target": "up", "count": 1, "required": 2}
    # While down only the recovery streak means something.
    assert state["fail_streak"] is None and state["ok_streak"] == 1 and state["clear_streak"] is None
    assert parse(state["status_since"]) == ago(minutes=2)
    flapping = body["flapping"]
    assert flapping["active"] is True and flapping["kind"] == "reachability"
    assert parse(flapping["since"]) == ago(minutes=10)
    assert flapping["reach_changes_in_window"] == 4 and flapping["perf_changes_in_window"] == 0
    assert (flapping["window_minutes"], flapping["start_changes"], flapping["stop_changes"],
            flapping["stable_minutes"], flapping["enabled"]) == (30, 4, 1, 10, True)
    # Last change 2 min ago + 10 stable minutes (sooner than the count stop at -10 + 30).
    assert parse(flapping["earliest_stop_at"]) == NOW + timedelta(minutes=8)
    # 4 reachability changes alternate 3 failed / 2 answered checks: 7 checks must fit in 30 min.
    assert flapping["max_detectable_interval_s"] == 257 and flapping["detectable"] is True
    # Evidence comes from the controller's own samples.
    poller = os.getenv("POLLER_ID", "poller-01")
    assert ch_calls and all(p.get("poller") == poller for p in ch_calls if "poller" in p)
    assert body["latest"] is None and body["recent_degraded"] is None


def test_explanation_reports_window_evidence_towards_degraded(world, ch_calls):
    with team_a(world) as c:
        state = explanation(c, "pending")["state"]
    assert state["hard_status"] == "up"
    assert state["pending"] == {"target": "degraded", "count": 2, "required": 3}
    assert (state["bad_in_window"], state["window_len"], state["window_size"]) == (2, 3, 5)
    # ok_streak lingers after the recovery that caused it; it is not reported while up.
    assert state["ok_streak"] is None and state["fail_streak"] == 0


def test_explanation_for_a_sensor_owned_device(world, ch_calls):
    with team_a(world) as c:
        body = explanation(c, "sensor")
    assert body["source"] == "branch-site"
    # The sensor's own down confirmation (2), not the central one (3).
    assert body["confirmation"]["failed_checks"] == 2 and body["confirmation"]["recovery_checks"] == 2
    assert body["state"]["seeded"] is False
    assert body["state"]["pending"] == {"target": "down", "count": 1, "required": 2}
    assert body["flapping"]["active"] is False and body["flapping"]["earliest_stop_at"] is None
    # Sensor reachability: 2 failed / 2 answered checks -> 2+2+2 = 6 checks per 30 min.
    assert body["flapping"]["max_detectable_interval_s"] == 299
    assert all(p.get("poller") == str(SENSOR_ID) for p in ch_calls if "poller" in p)


def test_explanation_shows_the_seed_for_a_row_of_another_owner(world, ch_calls):
    with team_a(world) as c:
        state = explanation(c, "reseed")["state"]
    assert state["seeded"] is True and state["hard_status"] == "degraded"
    assert state["pending"] is None and state["clear_streak"] == 0 and state["fail_streak"] == 0
    assert parse(state["status_since"]) == ago(hours=1)       # devices.status_changed_at, not the stale row


def test_explanation_freshness_sees_through_the_central_heartbeat(pg_db, monkeypatch):
    """The central poller rewrites an unchanged state row only every 10 minutes, so a steady device's row is
    minutes old while the poller checks it every interval: its freshness is the newest of the row, devices.last_seen
    and the controller's latest ping sample. A sensor rewrites its row with every accepted batch, and its last_seen
    can come from a sample the machine never took, so its row alone decides."""
    from app.services import device_status_explanation as module
    ids = {name: uuid.uuid4() for name in ("answering", "silent_down", "stopped", "sensor")}
    sensor_id = uuid.uuid4()
    poller = os.getenv("POLLER_ID", "poller-01")
    # name: (status, devices.last_seen, latest controller sample)
    devices = {"answering": ("up", ago(seconds=20), None),
               "silent_down": ("down", ago(hours=3), (ago(seconds=30), 0.0, 1.0, 0)),
               "stopped": ("up", ago(minutes=9), None),
               "sensor": ("up", ago(seconds=20), None)}
    for n, (name, (status, seen, _)) in enumerate(devices.items(), 1):
        pg_db.execute("INSERT INTO devices (id, hostname, ip_address, tags, status, ping_enabled, ping_interval,"
                      " last_seen) VALUES ($1, $2, $3::inet, '[]'::jsonb, $4, true, 60, $5)",
                      ids[name], f"beat-{name}", f"10.8.0.{n}", status, seen)
    pg_db.execute("INSERT INTO sensors (id, name, status) VALUES ($1, 'beat-site', 'online')", sensor_id)
    pg_db.execute("INSERT INTO sensor_assignments (sensor_id, target_type, target_id) VALUES ($1, 'device', $2)",
                  sensor_id, ids["sensor"])
    for name, owner, hard in (("answering", "central", "up"), ("silent_down", "central", "down"),
                              ("stopped", "central", "up"), ("sensor", str(sensor_id), "up")):
        # Written by the last heartbeat (or batch) 9 minutes ago; a clean, full window.
        pg_db.execute("INSERT INTO device_status_state (device_id, owner, hard_status, fail_streak, window_len,"
                      " last_sample_at) VALUES ($1, $2, $3, $4, 5, $5)",
                      ids[name], owner, hard, 100 if hard == "down" else 0, ago(minutes=9))

    samples = {str(ids[name]): sample for name, (_, _, sample) in devices.items() if sample}

    class Client:
        def query(self, sql, parameters=None, **kw):
            p = parameters or {}
            latest = "FROM ping_metrics" in sql and "ORDER BY timestamp DESC" in sql
            hit = samples.get(p.get("id")) if latest and p.get("poller") == poller else None
            return SimpleNamespace(result_rows=[hit] if hit else [])

    monkeypatch.setattr(module, "get_clickhouse_client", lambda: Client())
    with everyone(pg_db) as c:
        def state(name):
            resp = c.get(f"/api/v1/devices/{ids[name]}/status-explanation")
            assert resp.status_code == 200, resp.text[:300]
            return resp.json()["state"]

        answering = state("answering")
        assert answering["stale"] is False and answering["seeded"] is False
        assert parse(answering["last_sample_at"]) == ago(seconds=20)
        # Down for hours: no answer moves last_seen, but every unanswered check is a ping sample.
        silent_down = state("silent_down")
        assert silent_down["stale"] is False and parse(silent_down["last_sample_at"]) == ago(seconds=30)
        # Nothing newer than the 9-minute-old row: the poller really stopped checking it.
        stopped = state("stopped")
        assert stopped["stale"] is True and parse(stopped["last_sample_at"]) == ago(minutes=9)
        sensor = state("sensor")
        assert sensor["stale"] is True and parse(sensor["last_sample_at"]) == ago(minutes=9)


def test_explanation_without_ping_has_the_policy_but_no_state(world, ch_calls):
    with team_a(world) as c:
        body = explanation(c, "off")
    assert body["mode"] == "disabled" and body["state"] is None and body["flapping"] is None
    assert body["policy"]["down_confirm_checks"] == 3 and "confirmation" not in body
    assert ch_calls == []


def test_explanation_and_episodes_hide_devices_outside_the_scope(world, ch_calls):
    with team_a(world) as c:
        assert c.get(f"/api/v1/devices/{ID['hidden']}/status-explanation").status_code == 404
        assert c.get(f"/api/v1/devices/{ID['hidden']}/flap-episodes").status_code == 404
    with everyone(world) as c:
        assert c.get(f"/api/v1/devices/{ID['hidden']}/flap-episodes").status_code == 200
        assert c.get(f"/api/v1/devices/{uuid.uuid4()}/flap-episodes").status_code == 404


# ── flap episodes ───────────────────────────────────────────────────────────

def test_flap_episodes_newest_first_with_period_and_limit(world):
    with team_a(world) as c:
        body = c.get(f"/api/v1/devices/{ID['flap']}/flap-episodes").json()
        assert body["truncated"] is False
        open_, closed = body["items"]
        assert open_["ongoing"] is True and open_["ended_at"] is None and open_["kind"] == "reachability"
        assert open_["duration_s"] >= 600 and open_["end_reason"] is None
        assert closed["ongoing"] is False and closed["kind"] == "both" and closed["end_reason"] == "stable"
        assert closed["duration_s"] == 2400 and closed["status_at_end"] == "up"
        assert parse(closed["started_at"]) == ago(days=3)
        assert closed["start_notice"] == "pending" and closed["window_minutes"] == 30

        recent = c.get(f"/api/v1/devices/{ID['flap']}/flap-episodes",
                       params={"from": iso(ago(days=1)), "to": iso(NOW)}).json()
        assert [e["ongoing"] for e in recent["items"]] == [True]
        assert parse(recent["from"]) == ago(days=1)

        # An episode overlapping the period counts even though it started before it.
        overlap = c.get(f"/api/v1/devices/{ID['flap']}/flap-episodes",
                        params={"from": iso(ago(days=3) + timedelta(minutes=30)),
                                "to": iso(ago(days=2))}).json()
        assert [e["kind"] for e in overlap["items"]] == ["both"]

        limited = c.get(f"/api/v1/devices/{ID['flap']}/flap-episodes", params={"limit": 1}).json()
        assert len(limited["items"]) == 1 and limited["truncated"] is True

        assert c.get(f"/api/v1/devices/{ID['flap']}/flap-episodes",
                     params={"from": iso(NOW), "to": iso(ago(days=1))}).status_code == 422
        assert c.get(f"/api/v1/devices/{ID['pending']}/flap-episodes").json()["items"] == []


# ── report section query on real Postgres ───────────────────────────────────

def test_report_flap_episode_query_clips_to_the_period_and_honours_the_device_filter(world):
    """The device_flapping section's episode aggregate (report_packs/devices._flap_episodes) on the real schema."""
    import asyncio
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool
    from app.services.report_packs import devices as pack
    from app.services.report_sections import SectionCtx

    async def run(frm, to, filters=None):
        engine = create_async_engine(world.url, poolclass=NullPool)
        try:
            async with AsyncSession(engine) as session:
                return await pack._flap_episodes(SectionCtx(session, frm, to, filters))
        finally:
            await engine.dispose()

    # Naive UTC bounds, as the render API passes them.
    naive = lambda ts: ts.replace(tzinfo=None)  # noqa: E731
    week = asyncio.run(run(naive(ago(days=7)), naive(NOW)))
    flap = week[str(ID["flap"])]
    assert flap["episodes"] == 2 and flap["ongoing"] is True
    # 40 closed minutes plus the open episode up to now (about 10 minutes, still growing).
    assert 2400 + 590 <= flap["flapping_s"] <= 2400 + 900
    assert week[str(ID["hidden"])]["episodes"] == 1

    # Clipped to the period: half of the closed episode, the open one outside it.
    half = asyncio.run(run(naive(ago(days=3) + timedelta(minutes=20)), naive(ago(days=2))))
    assert half == {str(ID["flap"]): {"episodes": 1, "flapping_s": 1200.0, "ongoing": False}}

    scoped = asyncio.run(run(naive(ago(days=7)), naive(NOW), {"device_ids": [str(ID["hidden"])]}))
    assert set(scoped) == {str(ID["hidden"])}


# ── maintenance writers stamp status_changed_at ─────────────────────────────

def test_maintenance_create_and_delete_stamp_status_changed_at(pg_db):
    earlier, already = ago(days=1), ago(hours=2)
    devices = {uuid.uuid4(): ("up", True, earlier), uuid.uuid4(): ("maintenance", False, already),
               uuid.uuid4(): ("up", False, earlier)}
    for n, (did, (status, ping, changed)) in enumerate(devices.items(), 1):
        pg_db.execute("INSERT INTO devices (id, hostname, ip_address, tags, status, ping_enabled, status_changed_at)"
                      " VALUES ($1, $2, $3::inet, $4::jsonb, $5, $6, $7)",
                      did, f"maint-{n}", f"10.7.0.{n}", ["maint-tag"], status, ping, changed)
    pinged, in_maintenance, unpinged = devices

    def row(did):
        return pg_db.query("SELECT status, status_changed_at FROM devices WHERE id = $1", did)[0]

    with api_client(pg_db, role="t_flap_maint", permissions=["devices.view", "devices.manage"],
                    persist_user=True) as c:
        resp = c.post("/api/v1/device-maintenance", json={
            "scope_type": "tag", "scope_tag": "maint-tag", "starts_at": iso(ago(minutes=5)),
            "ends_at": iso(NOW + timedelta(hours=1)), "reason": "patching"})
        assert resp.status_code == 201, resp.text[:300]
        status, changed = row(pinged)
        assert status == "maintenance" and changed > NOW - timedelta(minutes=1)
        # Already in maintenance: no status change, so its change time stays.
        assert row(in_maintenance) == ("maintenance", already)

        entered = row(unpinged)[1]
        assert c.delete(f"/api/v1/device-maintenance/{resp.json()['id']}").status_code == 204
        # Ping-disabled devices have no status writer: the delete resets them to unknown, now.
        status, changed = row(unpinged)
        assert status == "unknown" and changed >= entered
        # Pinged devices are left to their writer, which confirms the post-window status itself.
        assert row(pinged)[0] == "maintenance"


@pytest.mark.parametrize("owner", ["sensor", "central"])
def test_disabling_ping_releases_the_flap_state(pg_db, owner):
    """Nobody checks a device again once ping is off (a remote-sensor owner never releases it; the central poller
    only notices at its next device sync), so the update itself clears the flap mirror, closes the open episode as
    not_polled and drops the state row, whichever writer owned it."""
    owner = str(SENSOR_ID) if owner == "sensor" else "central"
    dev = uuid.uuid4()
    pg_db.execute("INSERT INTO devices (id, hostname, ip_address, status, ping_enabled, is_flapping,"
                  " flapping_since, flap_kind) VALUES ($1, 'flappy-off', '10.9.9.9', 'down', true, true, $2,"
                  " 'reachability')", dev, ago(minutes=8))
    pg_db.execute("INSERT INTO device_status_state (device_id, owner, hard_status, reach_flapping, flapping_since)"
                  " VALUES ($1, $2, 'down', true, $3)", dev, owner, ago(minutes=8))
    pg_db.execute("INSERT INTO device_flap_episodes (device_id, started_at, kind, start_changes, window_minutes,"
                  " status_at_start) VALUES ($1, $2, 'reachability', 4, 30, 'up')", dev, ago(minutes=8))
    with api_client(pg_db, role="t_flap_manage", permissions=["devices.view", "devices.manage"]) as c:
        resp = c.put(f"/api/v1/devices/{dev}", json={"ping_enabled": False})
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["is_flapping"] is False and body["flapping_since"] is None and body["flap_kind"] is None
    assert pg_db.scalar("SELECT count(*) FROM device_status_state WHERE device_id = $1", dev) == 0
    [(ended, reason, at_end)] = pg_db.query(
        "SELECT ended_at, end_reason, status_at_end FROM device_flap_episodes WHERE device_id = $1", dev)
    assert ended is not None and reason == "not_polled" and at_end == "down"


# ── saving settings vs the historical breach evidence ───────────────────────

def test_explanation_keeps_breach_evidence_until_a_limit_really_changes(pg_db, monkeypatch):
    """The settings card saves the full document, which moves system_settings.updated_at even when only the
    confirmation or flapping settings change. Only a change of the limits a sample is judged against may hide a
    degraded event's breaches as "judged against other limits" or mark the latest sample as settings-pending."""
    from app.services import device_status_explanation as module
    dev = uuid.uuid4()
    event_at, latest_at = ago(hours=2), ago(seconds=30)
    pg_db.execute("INSERT INTO devices (id, hostname, ip_address, tags, status, ping_enabled, ping_interval)"
                  " VALUES ($1, 'wan-history', '10.6.0.1', '[]'::jsonb, 'up', true, 60)", dev)
    # Written before the threshold stamp existed, days before the event.
    pg_db.execute("INSERT INTO system_settings (key, value, updated_at) VALUES ('monitoring', $1::jsonb, $2)"
                  " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                  {"degraded_rtt_ms": 150, "degraded_loss_pct": 10}, ago(days=3))

    class Client:
        def query(self, sql, parameters=None, **kw):
            if "FROM device_status_log" in sql and "new_status = 'degraded'" in sql:
                rows = [(event_at, "Latency 180.000 ms exceeds 150 ms by 30.000 ms")]
            elif "FROM ping_metrics" in sql and "dateDiff" in sql:
                rows = [(event_at, 180.0, 0.0, 1)]
            elif "FROM ping_metrics" in sql:
                rows = [(latest_at, 20.0, 0.0, 1)]
            else:
                rows = []
            return SimpleNamespace(result_rows=rows)

    monkeypatch.setattr(module, "get_clickhouse_client", lambda: Client())
    with api_client(pg_db, role="t_flap_settings", permissions=["devices.view", "settings.view", "settings.manage"],
                    persist_user=True) as c:
        def explained():
            resp = c.get(f"/api/v1/devices/{dev}/status-explanation")
            assert resp.status_code == 200, resp.text[:300]
            return resp.json()

        def save(document):
            resp = c.put("/api/v1/settings/monitoring", json=document)
            assert resp.status_code == 200, resp.text[:300]

        full = {k: v for k, v in c.get("/api/v1/settings/monitoring").json().items() if k != "effective"}
        for change in ({}, {"flap_window_minutes": 60, "flap_stable_minutes": 20},
                       {"degraded_confirm_checks": 2, "down_confirm_checks": 4, "up_confirm_checks": 2}):
            save({**full, **change})
            body = explained()
            recent = body["recent_degraded"]
            assert recent["thresholds_unchanged"] is True, change
            assert recent["sample"]["classification"] == "breach"
            assert [(b["metric"], b["threshold"]) for b in recent["sample"]["breaches"]] == [("Latency", 150)]
            assert body["latest"]["settings_pending"] is False

        save({"degraded_rtt_ms": 200})
        body = explained()
        recent = body["recent_degraded"]
        assert recent["thresholds_unchanged"] is False
        assert recent["sample"]["breaches"] == [] and recent["sample"]["classification"] is None
        assert recent["reason"] == "Latency 180.000 ms exceeds 150 ms by 30.000 ms"     # as recorded
        assert body["latest"]["settings_pending"] is True
