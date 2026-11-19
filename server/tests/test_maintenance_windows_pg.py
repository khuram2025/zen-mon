"""Recurring maintenance windows and server scope (migrate-207, plan G-6).

On a customer appliance CPU/memory alerts peaked at 02:00 every night (the
backups) and paged as P1: windows were one-off and devices only. Now:

* ``device_maintenance_occurrences`` / ``device_maintenance_active`` (SQL) is
  the one definition of when a window is in force, and the Python mirror the
  API uses gives the same answers, across DST changes too;
* one-off windows behave exactly as before;
* server, server group and server tag windows (and a window on a server's
  linked device) put a server in maintenance for server availability, the
  monitoring gap evaluator and the staleness sweep; Device Tracker honours
  its switch's maintenance;
* the API creates recurring and server windows with validation, the extra
  servers.manage permission and an audit row, and lists the next occurrence.
"""
from __future__ import annotations

import contextlib
from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import maintenance_windows as mw
from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

UTC = timezone.utc
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
ALL_DAYS = [1, 2, 3, 4, 5, 6, 7]
MANAGER = ["devices.view", "devices.manage", "servers.view"]


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


def add_device(db, name, ip, *, tags=(), status="up", group=None):
    return str(db.scalar(
        "INSERT INTO devices (hostname, ip_address, status, tags, group_id)"
        " VALUES ($1, $2::inet, $3, $4::jsonb, $5) RETURNING id", name, ip, status, list(tags), group))


def add_server(db, name, *, tags=(), device=None, heartbeat=None):
    sid = str(db.scalar(
        "INSERT INTO servers (display_name, hostname, collection_mode, status, tags, device_id)"
        " VALUES ($1, $1, 'agent', 'healthy', $2::jsonb, $3) RETURNING id", name, list(tags), device))
    db.execute("INSERT INTO agents (server_id, agent_uid, hostname, status, last_heartbeat_at, last_metric_at)"
               " VALUES ($1, $2, $3, 'online', $4, $4)", sid, f"uid-{name}", name,
               heartbeat or datetime.now(UTC))
    return sid


def add_group(db, name, kinds=("device", "server")):
    return str(db.scalar("INSERT INTO device_groups (name, asset_kinds) VALUES ($1, $2) RETURNING id",
                         name, list(kinds)))


def add_window(db, scope_type, *, recurring_now=False, recurring_later=False, one_off_now=False, **scope):
    """A window of the given scope: in force now (recurring or one-off) or
    recurring but between occurrences."""
    cols = ", ".join(scope)
    vals = ", ".join(f"${i + 2}" for i in range(len(scope)))
    lead = f", {cols}" if scope else ""
    tail = f", {vals}" if scope else ""
    if one_off_now:
        return str(db.scalar(
            f"INSERT INTO device_maintenance (scope_type{lead}, starts_at, ends_at) VALUES ($1{tail},"
            " now() - interval '5 minutes', now() + interval '5 minutes') RETURNING id", scope_type, *scope.values()))
    offset = "- interval '10 minutes'" if recurring_now else "+ interval '3 hours'"
    assert recurring_now or recurring_later
    return str(db.scalar(
        f"INSERT INTO device_maintenance (scope_type{lead}, starts_at, ends_at, recur_days, recur_start_time,"
        f" recur_duration_minutes, recur_timezone) VALUES ($1{tail}, now() - interval '3 days',"
        f" now() - interval '3 days' + interval '1 hour', '{{1,2,3,4,5,6,7}}',"
        f" (now() AT TIME ZONE 'UTC' {offset})::time, 60, 'UTC') RETURNING id", scope_type, *scope.values()))


# ── The SQL function and its Python mirror agree ────────────────────────────

CASES = [
    # (days, start, minutes, tz, until, range start, range end)
    (ALL_DAYS, "02:00", 60, "Europe/Berlin", None, datetime(2026, 3, 27, tzinfo=UTC), datetime(2026, 3, 31, tzinfo=UTC)),
    (ALL_DAYS, "02:30", 30, "Europe/Berlin", None, datetime(2026, 3, 27, tzinfo=UTC), datetime(2026, 3, 31, tzinfo=UTC)),
    (ALL_DAYS, "02:30", 30, "Europe/Berlin", None, datetime(2026, 10, 23, tzinfo=UTC), datetime(2026, 10, 27, tzinfo=UTC)),
    (ALL_DAYS, "01:30", 120, "Europe/Berlin", None, datetime(2026, 3, 28, tzinfo=UTC), datetime(2026, 3, 30, tzinfo=UTC)),
    (ALL_DAYS, "01:30", 90, "America/New_York", None, datetime(2026, 10, 31, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC)),
    ([6], "23:00", 180, "UTC", None, datetime(2026, 11, 16, tzinfo=UTC), datetime(2026, 11, 30, tzinfo=UTC)),
    ([1], "00:00", 10080, "Asia/Riyadh", None, datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 30, tzinfo=UTC)),
    ([2, 4], "22:45", 200, "Europe/Berlin", date(2026, 11, 19), datetime(2026, 11, 10, tzinfo=UTC), datetime(2026, 11, 30, tzinfo=UTC)),
]


def test_sql_and_python_agree_on_occurrences_across_dst(pg_db):
    for days, start, minutes, tz, until, frm, to in CASES:
        r = mw.Recurrence(days=tuple(days), start_time=time.fromisoformat(start), duration_minutes=minutes,
                          timezone=tz, until=until)
        sql = pg_db.query(
            "SELECT occ_start, occ_end FROM device_maintenance_occurrences($1, $2, $3::smallint[], $4, $5, $6, $7, $8, $9)"
            " ORDER BY 1", EPOCH, EPOCH, days, time.fromisoformat(start), minutes, tz, until, frm, to)
        assert [tuple(x) for x in sql] == mw.occurrences(EPOCH, EPOCH, r, frm, to), (days, start, tz)
        # Every 10 minutes across the range: in force or not, both agree.
        instants = [frm + timedelta(minutes=10 * i) for i in range(int((to - frm).total_seconds() // 600) + 1)]
        flags = pg_db.query(
            "SELECT EXISTS (SELECT 1 FROM device_maintenance_occurrences($1, $2, $3::smallint[], $4, $5, $6, $7, t, t))"
            " FROM unnest($8::timestamptz[]) WITH ORDINALITY AS u(t, n) ORDER BY n",
            EPOCH, EPOCH, days, time.fromisoformat(start), minutes, tz, until, instants)
        assert [f[0] for f in flags] == [mw.active_at(EPOCH, EPOCH, r, t) for t in instants], (days, start, tz)


def test_one_off_window_through_the_sql_function(pg_db):
    s, e = datetime(2026, 11, 19, 2, tzinfo=UTC), datetime(2026, 11, 19, 3, tzinfo=UTC)
    q = ("SELECT count(*) FROM device_maintenance_occurrences($1, $2, NULL, NULL, NULL, NULL, NULL, $3, $4)")
    assert pg_db.scalar(q, s, e, e, e) == 1                         # inclusive end, as before
    assert pg_db.scalar(q, s, e, s, s) == 1
    assert pg_db.scalar(q, s, e, e + timedelta(seconds=1), e + timedelta(hours=1)) == 0


def test_migration_is_idempotent(pg_db):
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "migrate-207-recurring-maintenance.sql"
    before = pg_db.scalar("SELECT count(*) FROM pg_constraint WHERE conrelid = 'device_maintenance'::regclass")
    pg_db.psql_file(str(path))
    assert pg_db.scalar("SELECT count(*) FROM pg_constraint WHERE conrelid = 'device_maintenance'::regclass") == before


def test_migration_refuses_bad_rows(pg_db):
    db = pg_db
    dev = add_device(db, "sw-1", "192.0.2.1")
    with pytest.raises(Exception, match="time zone"):
        db.execute("INSERT INTO device_maintenance (scope_type, scope_device_id, starts_at, ends_at, recur_days,"
                   " recur_start_time, recur_duration_minutes, recur_timezone) VALUES ('device', $1, now(),"
                   " now() + interval '1 hour', '{1}', '02:00', 60, 'Mars/Olympus')", dev)
    for days, minutes in (("{8}", 60), ("{1}", 0), ("{1}", 10081), ("{}", 60)):
        with pytest.raises(Exception):
            db.execute("INSERT INTO device_maintenance (scope_type, scope_device_id, starts_at, ends_at,"
                       " recur_days, recur_start_time, recur_duration_minutes, recur_timezone) VALUES ('device', $1,"
                       f" now(), now() + interval '1 hour', '{days}', '02:00', $2, 'UTC')", dev, minutes)
    with pytest.raises(Exception):      # a recurrence is all or nothing
        db.execute("INSERT INTO device_maintenance (scope_type, scope_device_id, starts_at, ends_at, recur_days)"
                   " VALUES ('device', $1, now(), now() + interval '1 hour', '{1}')", dev)
    with pytest.raises(Exception):
        db.execute("INSERT INTO device_maintenance (scope_type, starts_at, ends_at) VALUES ('server', now(),"
                   " now() + interval '1 hour')")
    # A deleted server takes its windows with it.
    srv = add_server(db, "app-1")
    add_window(db, "server", one_off_now=True, scope_server_id=srv)
    db.execute("DELETE FROM servers WHERE id = $1", srv)
    assert db.scalar("SELECT count(*) FROM device_maintenance WHERE scope_type = 'server'") == 0


# ── Evaluator helpers ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_device_in_maintenance_for_recurring_windows_of_every_scope(pg_db):
    from app.api.v1.alert_engine import _device_in_maintenance
    db = pg_db
    group = add_group(db, "branch-routers")
    by_device = add_device(db, "r-device", "192.0.2.10")
    by_group = add_device(db, "r-group", "192.0.2.11", group=group)
    by_tag = add_device(db, "r-tag", "192.0.2.12", tags=["backup"])
    between = add_device(db, "r-between", "192.0.2.13")
    one_off = add_device(db, "r-one-off", "192.0.2.14")
    add_window(db, "device", recurring_now=True, scope_device_id=by_device)
    add_window(db, "group", recurring_now=True, scope_group_id=group)
    add_window(db, "tag", recurring_now=True, scope_tag="backup")
    add_window(db, "device", recurring_later=True, scope_device_id=between)
    add_window(db, "device", one_off_now=True, scope_device_id=one_off)
    async with session(db) as s:
        for did in (by_device, by_group, by_tag, one_off):
            assert await _device_in_maintenance(s, did), did
        assert not await _device_in_maintenance(s, between)
        assert await mw.active_device_ids(s) == {by_device, by_group, by_tag, one_off}


@pytest.mark.asyncio
async def test_server_in_maintenance_by_server_group_tag_and_linked_device(pg_db):
    db = pg_db
    group = add_group(db, "backup-servers", kinds=("server",))
    linked_dev = add_device(db, "esx-01", "192.0.2.20")
    direct = add_server(db, "app-direct")
    in_group = add_server(db, "app-group")
    db.execute("INSERT INTO device_group_members (group_id, asset_kind, asset_id, source)"
               " VALUES ($1, 'server', $2, 'static')", group, in_group)
    tagged = add_server(db, "app-tagged", tags=["nightly-backup"])
    linked = add_server(db, "app-linked", device=linked_dev)
    between = add_server(db, "app-between")
    untouched = add_server(db, "app-untouched", tags=["backup"])
    add_window(db, "server", recurring_now=True, scope_server_id=direct)
    add_window(db, "server_group", recurring_now=True, scope_group_id=group)
    add_window(db, "server_tag", recurring_now=True, scope_tag="nightly-backup")
    add_window(db, "device", one_off_now=True, scope_device_id=linked_dev)
    add_window(db, "server", recurring_later=True, scope_server_id=between)
    # A device tag window does not cover a server that carries the same tag.
    add_window(db, "tag", recurring_now=True, scope_tag="backup")
    async with session(db) as s:
        for sid in (direct, in_group, tagged, linked):
            assert await mw.server_in_maintenance(s, sid), sid
        for sid in (between, untouched):
            assert not await mw.server_in_maintenance(s, sid), sid
        assert await mw.active_server_ids(s) == {direct, in_group, tagged, linked}


@pytest.mark.asyncio
async def test_server_availability_reads_a_server_window_as_maintenance(pg_db):
    from app.services import server_availability as sa
    db = pg_db
    silent = datetime.now(UTC) - timedelta(minutes=30)
    srv = add_server(db, "db-01", heartbeat=silent)
    other = add_server(db, "db-02", heartbeat=silent)
    add_window(db, "server", recurring_now=True, scope_server_id=srv)
    async with session(db) as s:
        signals = await sa.load_signals(s)
        one = await sa.load_signals(s, srv)
    now = datetime.now(UTC).timestamp()
    assert signals[srv].maintenance and one[srv].maintenance and not signals[other].maintenance
    assert sa.judge(signals[srv], now, 180) == "maintenance"
    assert sa.judge(signals[other], now, 180) == "down"
    # Nobody pages a server in maintenance, built-in fallback included.
    assert sa.covered([], signals[srv], "maintenance")


@pytest.mark.asyncio
async def test_agent_no_metrics_skips_a_server_in_a_window(pg_db, monkeypatch):
    from app.services import host_alert_service
    from app.services import monitoring_gap_alert_service as gap
    db = pg_db
    sent = []

    async def dispatch(_db, channel_ids, ctx):
        sent.append(ctx)
        return len(channel_ids)

    monkeypatch.setattr(host_alert_service, "dispatch_to_channels", dispatch)
    now = datetime.now(UTC)
    srv = add_server(db, "web-01")
    other = add_server(db, "web-02")
    db.execute("UPDATE agents SET last_metric_at = $1", now - timedelta(minutes=40))
    rule = str(db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, notify_channels)"
        " VALUES ('No metrics', 'agent_no_metrics', '>=', 20, 'warning', '[]'::jsonb) RETURNING id"))
    add_window(db, "server", recurring_now=True, scope_server_id=srv)
    async with session(db) as s:
        await gap.evaluate_monitoring_gap_rules(s, now=now)
        await s.commit()
    raised = [r[0] for r in db.query("SELECT server_id::text FROM alerts WHERE rule_id = $1", rule)]
    assert raised == [other]


@pytest.mark.asyncio
async def test_device_tracker_consumes_events_of_a_switch_in_maintenance(pg_db):
    from app.services import alert_funnel
    from app.services import udt_alert_service as udt
    alert_funnel.reset_for_tests()
    db = pg_db
    sw = add_device(db, "access-1", "192.0.2.30")
    rule = str(db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, notify_channels, cooldown)"
        " VALUES ('Rogue', 'udt_rogue_endpoint', '>=', 1, 'warning', '[]'::jsonb, 300) RETURNING id"))
    ep = str(db.scalar("INSERT INTO udt_endpoints (mac, hostname) VALUES ('aa:bb:cc:00:00:09', 'laptop')"
                       " RETURNING id"))
    window = add_window(db, "device", recurring_now=True, scope_device_id=sw)

    async def run():
        db.execute("INSERT INTO udt_events (event_type, endpoint_id, device_id) VALUES ('rogue_detected', $1, $2)",
                   ep, sw)
        async with session(db) as s:
            raised = await udt._process_events(s, await udt._udt_rules(s), [])
            await s.commit()
        return raised

    assert await run() == 0
    assert db.scalar("SELECT count(*) FROM udt_events WHERE NOT alerted") == 0
    assert db.scalar("SELECT count(*) FROM alerts WHERE rule_id = $1", rule) == 0
    db.execute("DELETE FROM device_maintenance WHERE id = $1", window)
    assert await run() == 1


@pytest.mark.asyncio
async def test_report_intervals_expand_recurring_windows(pg_db):
    from app.services.report_service import _fetch_device_maintenance_windows
    db = pg_db
    dev = add_device(db, "core-1", "192.0.2.40")
    first = datetime(2026, 11, 2, 1, tzinfo=UTC)                 # Monday 02:00 Berlin
    db.execute("INSERT INTO device_maintenance (scope_type, scope_device_id, starts_at, ends_at, recur_days,"
               " recur_start_time, recur_duration_minutes, recur_timezone) VALUES ('device', $1, $2, $3,"
               " '{1,2,3,4,5,6,7}', '02:00', 60, 'Europe/Berlin')", dev, first, first + timedelta(hours=1))
    async with session(db) as s:
        got = await _fetch_device_maintenance_windows(
            s, datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 8, tzinfo=UTC), strict=True)
    days = sorted(got[dev])
    assert len(days) == 6                                         # 2nd .. 7th: the series began on the 2nd
    assert days[0] == (datetime(2026, 11, 2, 1), datetime(2026, 11, 2, 2))

    # The Planned Maintenance report section lists each occurrence and
    # excludes one device-hour per night.
    from app.services import report_sections as rs
    from app.services.branding import Branding
    async with session(db) as s:
        [sec] = await rs.build_sections(s, [{"id": "maintenance_summary"}], datetime(2026, 11, 1, tzinfo=UTC),
                                        datetime(2026, 11, 8, tzinfo=UTC), None, requester=None,
                                        branding=Branding(timezone="Europe/Berlin"))
    assert not sec.get("error"), sec.get("notes")
    assert len(sec["tables"][0]["rows"]) == 6
    assert next(k for k in sec["kpis"] if k["label"] == "Device-hours excluded")["value"] == "6.0 h"

    # The device page's availability and uptime queries read occurrences too.
    from app.api.v1.devices import MAINT_COVERS_DEVICE_SQL
    async with session(db) as s:
        rows = (await s.execute(text(f"""
            SELECT d.id::text, GREATEST(o.occ_start, :from_ts) AS s, LEAST(o.occ_end, :to_ts) AS e
            FROM device_maintenance m {mw.occurrences_join(":from_ts", ":to_ts")}
            JOIN devices d ON {MAINT_COVERS_DEVICE_SQL}
            WHERE o.occ_start < :to_ts AND o.occ_end > :from_ts
        """), {"from_ts": datetime(2026, 11, 3, 1, 30, tzinfo=UTC),
               "to_ts": datetime(2026, 11, 4, tzinfo=UTC)})).all()
    assert [(r[1], r[2]) for r in rows] == [(datetime(2026, 11, 3, 1, 30, tzinfo=UTC),
                                             datetime(2026, 11, 3, 2, tzinfo=UTC))]


# ── API ─────────────────────────────────────────────────────────────────────

def manager(db, perms=MANAGER, **kw):
    return api_client(db, role=kw.pop("role", "maint-manager"), permissions=perms, persist_user=True,
                      username=kw.pop("username", "noc-lead"), **kw)


def test_api_creates_a_recurring_window_in_the_organisation_zone(pg_db):
    db = pg_db
    db.execute("INSERT INTO system_settings (key, value) VALUES ('company', '{\"timezone\": \"Europe/Berlin\"}')"
               " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    dev = add_device(db, "backup-host", "192.0.2.50")
    with manager(db) as c:
        r = c.post("/api/v1/device-maintenance", json={
            "scope_type": "device", "scope_device_id": dev, "reason": "Nightly backup",
            "recurrence": {"days": ALL_DAYS, "start_time": "02:00", "duration_minutes": 60}})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["recurring"] and body["recurrence"]["timezone"] == "Europe/Berlin"
        assert body["schedule"] == "Every day at 02:00 for 1 h (Europe/Berlin)"
        assert body["next_starts_at"] is not None
        # The stored first occurrence is a real one: 02:00 Berlin.
        starts = datetime.fromisoformat(body["starts_at"])
        assert starts.astimezone(mw.ZoneInfo("Europe/Berlin")).time() == time(2, 0)
        assert datetime.fromisoformat(body["ends_at"]) - starts == timedelta(hours=1)

        # Listed for the device long after its first occurrence ended.
        db.execute("UPDATE device_maintenance SET starts_at = starts_at - interval '14 days',"
                   " ends_at = ends_at - interval '14 days'")
        listed = c.get(f"/api/v1/devices/{dev}/maintenance").json()
        assert [w["id"] for w in listed["active"] + listed["upcoming"]] == [body["id"]]

        [(action, meta)] = db.query("SELECT action, metadata FROM audit_logs WHERE resource_id = $1", body["id"])
        assert action == "create" and meta["recurrence"]["timezone"] == "Europe/Berlin"
        assert meta["recurrence"]["days"] == ALL_DAYS and meta["reason"] == "Nightly backup"

        bad = c.post("/api/v1/device-maintenance", json={
            "scope_type": "device", "scope_device_id": dev,
            "recurrence": {"days": [1], "start_time": "02:00", "duration_minutes": 60, "timezone": "Nowhere/Land"}})
        assert bad.status_code == 422
        never = c.post("/api/v1/device-maintenance", json={
            "scope_type": "device", "scope_device_id": dev,
            "recurrence": {"days": [7], "start_time": "02:00", "duration_minutes": 60, "until": "2020-01-05"}})
        assert never.status_code == 422

        assert c.delete(f"/api/v1/device-maintenance/{body['id']}").status_code == 204
    assert db.scalar("SELECT count(*) FROM device_maintenance") == 0


def test_api_one_off_window_is_unchanged(pg_db):
    db = pg_db
    dev = add_device(db, "fw-1", "192.0.2.51")
    now = datetime.now(UTC)
    with manager(db) as c:
        r = c.post("/api/v1/device-maintenance", json={
            "scope_type": "device", "scope_device_id": dev,
            "starts_at": now.isoformat(), "ends_at": (now + timedelta(hours=1)).isoformat()})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["active"] and not body["recurring"] and body["schedule"] == "One-off"
    # In force now: the device shows Maintenance at once, as before.
    assert db.scalar("SELECT status FROM devices WHERE id = $1", dev) == "maintenance"
    assert db.scalar("SELECT recur_days FROM device_maintenance") is None


def test_api_server_window_needs_servers_manage(pg_db):
    db = pg_db
    srv = add_server(db, "app-01")
    payload = {"scope_type": "server", "scope_server_id": srv,
               "recurrence": {"days": [6, 7], "start_time": "01:00", "duration_minutes": 240, "timezone": "UTC"}}
    with manager(db) as c:
        assert c.post("/api/v1/device-maintenance", json=payload).status_code == 403
    with manager(db, perms=MANAGER + ["servers.manage"], role="maint-server-manager") as c:
        r = c.post("/api/v1/device-maintenance", json=payload)
        assert r.status_code == 201, r.text
        assert r.json()["scope_label"] == "app-01"
        listed = c.get(f"/api/v1/device-maintenance/servers/{srv}").json()
        assert [w["id"] for w in listed["active"] + listed["upcoming"]] == [r.json()["id"]]
        missing = c.post("/api/v1/device-maintenance", json={
            **payload, "scope_server_id": "00000000-0000-0000-0000-000000000001"})
        assert missing.status_code == 422
        devices_only = add_group(db, "switches", kinds=("device",))
        assert c.post("/api/v1/device-maintenance", json={
            "scope_type": "server_group", "scope_group_id": devices_only,
            "recurrence": payload["recurrence"]}).status_code == 422
        window = r.json()["id"]
    # Removing a server window needs servers.manage as well.
    with manager(db) as c:
        assert c.delete(f"/api/v1/device-maintenance/{window}").status_code == 403
    assert db.scalar("SELECT count(*) FROM device_maintenance") == 1
