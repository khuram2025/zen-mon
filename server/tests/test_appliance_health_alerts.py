"""ZenPlus appliance health alerts (alerting plan G-1, migrate-198).

The appliance raised nothing about itself: a full data disk, ClickHouse not
answering, failed backups or updates, an expiring licence, an unsynchronised
clock, a notification channel failing 180 deliveries in 30 days, a remote
sensor offline (a rule-less row that paged nobody).

Pinned here: the pure hold/clear lifecycle, each source's parsing with faked
inputs, the migration's seeds and its retirement of the rule-less sensor
rows, the evaluator end to end on a disposable PostgreSQL (with faked
sources and a recording dispatcher), and the rule API validator.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import alert_rule_scope as rs
from app.services import appliance_alert_service as apl
from app.services import host_alert_service as has
from app.services import storage_policy as P

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

T0 = 1_800_000_000.0          # synthetic epoch seconds
MIGRATION = Path(__file__).parents[2] / "scripts" / "migrate-198-appliance-health-alerts.sql"
CH_A = "00000000-0000-0000-0000-0000000000a1"
CH_B = "00000000-0000-0000-0000-0000000000b2"


def rule(metric, operator=">", threshold=0, **kw):
    return NS(id="r1", name=kw.pop("name", metric), metric=metric, operator=operator,
              threshold=threshold, conditions=kw.pop("conditions", None), target=kw.pop("target", None),
              **kw)


# ══ Holds and clear levels (pure) ════════════════════════════════════════════

def run_steps(values, *, hold_s=0, clear_s=300, cond=None, start=T0, every=60, open_=False):
    """Replay readings one pass apart; returns the actions."""
    cond = cond or apl.condition(rule(apl.DISK, ">", 85))
    h, actions = apl.Hold(), []
    for i, v in enumerate(values):
        now = start + i * every
        h, action = apl.step(h, now=now, value=v, breach=apl.breaches(cond, v),
                             cleared=apl.clears(cond, v), open_=open_, hold_s=hold_s, clear_s=clear_s)
        actions.append(action)
        if action == "raise":
            open_ = True
        elif action == "resolve":
            open_ = False
    return actions


def test_breach_hold_needs_the_condition_for_min_duration():
    assert run_steps([90, 90, 90], hold_s=120) == ["none", "none", "raise"]
    # A dip restarts the hold.
    assert run_steps([90, 90, 80, 90, 90], hold_s=120) == ["none"] * 5
    assert run_steps([90], hold_s=0) == ["raise"]


def test_disk_clears_two_points_below_the_trigger_and_after_the_clear_hold():
    cond = apl.condition(rule(apl.DISK, ">", 85))
    assert apl.reset_level(cond) == 83
    assert apl.clears(cond, 84) is False and apl.clears(cond, 83) is True
    # Open incident hovering at 84%: never resolves.
    assert run_steps([84] * 10, open_=True) == ["none"] * 10
    # Clear for 300 s (6 passes, the 6th at +300 s).
    assert run_steps([80] * 6, open_=True) == ["none"] * 5 + ["resolve"]
    # A re-breach inside the clear hold continues the incident.
    assert run_steps([80, 80, 90, 80, 80, 80, 80, 80], open_=True) == ["none"] * 8
    assert run_steps([80], open_=True, clear_s=0) == ["resolve"]


def test_explicit_reset_wins_and_lower_is_worse_metrics_clear_upwards():
    cond = apl.condition(rule(apl.DISK, ">", 85, conditions=[
        {"metric": apl.DISK, "operator": ">", "threshold": 85, "reset_threshold": 70}]))
    assert apl.reset_level(cond) == 70 and apl.clears(cond, 75) is False
    lic = apl.condition(rule(apl.LICENSE, "<", 30))
    assert apl.reset_level(lic) == 30          # no automatic band for days
    assert apl.breaches(lic, 12) and apl.clears(lic, 29.5) is False and apl.clears(lic, 365)


def test_missing_readings_never_resolve_or_count_as_clear_time():
    # The gap restarts the 300 s clear hold: 6 clean passes after it.
    assert run_steps([80, None] + [80] * 6, open_=True) == ["none"] * 7 + ["resolve"]
    assert run_steps([None] * 20, open_=True) == ["none"] * 20
    # ... nor as breach time.
    assert run_steps([90, None, 90, 90], hold_s=120) == ["none"] * 4


def test_a_gap_between_passes_restarts_the_holds():
    cond = apl.condition(rule(apl.DISK, ">", 85))
    h, _ = apl.step(apl.Hold(), now=T0, value=90, breach=True, cleared=False, open_=False,
                    hold_s=600, clear_s=300)
    h, action = apl.step(h, now=T0 + 3600, value=90, breach=True, cleared=False, open_=False,
                         hold_s=600, clear_s=300)
    assert action == "none" and h.breach_since == T0 + 3600
    assert apl.breaches(cond, None) is None


# ══ Sources (faked inputs) ═══════════════════════════════════════════════════

def fs(mount, used, total=100, avail=None, dev=1):
    return P.FsSample(mount=mount, device=dev, total_bytes=total, used_bytes=used,
                      avail_bytes=total - used if avail is None else avail)


def test_disk_reads_the_live_watchdog_data_volume():
    state = {"heartbeat": T0 - 30, "filesystems": [
        {"mount": "/", "usage_pct": 40.0, "holds_clickhouse": False},
        {"mount": "/data", "usage_pct": 91.25, "holds_clickhouse": True, "avail_bytes": 5}]}
    r = apl.disk_reading(state, None, T0)
    assert r.value == 91.25 and r.facts["mount"] == "/data"
    # One filesystem for / and /data (the root LV backs /data).
    shared = {"heartbeat": T0, "filesystems": [{"mount": "/ /data", "usage_pct": 70.0}]}
    assert apl.disk_reading(shared, None, T0).value == 70.0


def test_disk_falls_back_to_a_direct_sample_when_the_watchdog_is_stale():
    stale = {"heartbeat": T0 - 600, "filesystems": [{"mount": "/data", "usage_pct": 10.0}]}
    r = apl.disk_reading(stale, [fs("/", 50), fs("/data", 92, dev=2)], T0)
    assert r.value == 92.0
    assert apl.disk_reading(None, [], T0).value is None


def test_backup_failed_overdue_or_fine():
    daily = {"enabled": True, "frequency": "daily"}
    assert apl.backup_reading({"enabled": False}, {"status": "failed"}, None, None, T0).value == 0
    failed = apl.backup_reading(daily, {"status": "failed", "error": "disk full on 10.0.0.9"},
                                T0 - 3600, None, T0)
    assert failed.value == 1 and failed.facts["reason"] == "failed"
    assert "10.0.0.9" not in failed.facts["error"]
    overdue = apl.backup_reading(daily, {"status": "completed"}, T0 - 30 * 3600, None, T0)
    assert overdue.value == 1 and overdue.facts["reason"] == "overdue" and overdue.facts["hours_since"] == 30
    assert apl.backup_reading(daily, {"status": "completed"}, T0 - 20 * 3600, None, T0).value == 0
    # A schedule saved an hour ago is not overdue yet, even with no backup ever.
    assert apl.backup_reading(daily, None, None, T0 - 3600, T0).value == 0
    weekly = {"enabled": True, "frequency": "weekly"}
    assert apl.backup_reading(weekly, None, T0 - 100 * 3600, None, T0).value == 0
    assert apl.backup_reading(daily, None, None, None, T0).value is None


def test_update_reads_the_last_finished_attempt():
    history = [{"version": "1.25.16", "status": "success"},
               {"version": "1.25.17", "from_version": "1.25.16", "status": "rolled_back",
                "error": "schema gate failed, see https://zentryc.example/x?token=s3cr3t"},
               {"version": "1.25.18", "status": "downloading"}]
    r = apl.update_reading(history)
    assert r.value == 1 and r.facts["version"] == "1.25.17" and r.facts["status"] == "rolled_back"
    assert "s3cr3t" not in r.facts["error"] and "zentryc" not in r.facts["error"]
    assert apl.update_reading(history + [{"version": "1.25.18", "status": "success"}]).value == 0
    assert apl.update_reading(None).value is None
    assert apl.update_reading([{"status": "applying"}]).value is None


def test_licence_days_prefer_the_synchronised_subscription():
    exp = datetime.fromtimestamp(T0 + 12.5 * 86400, timezone.utc).isoformat()
    r = apl.license_reading({"plan": "professional", "expires_at": exp}, None, T0)
    assert r.value == 12.5 and r.facts["source"] == "zentryc"
    local = datetime.fromtimestamp(T0 - 2 * 86400, timezone.utc)
    assert apl.license_reading(None, local, T0).value == -2
    assert apl.license_reading({"plan": "enterprise", "expires_at": None}, local, T0).value \
        == apl.LICENSE_NO_EXPIRY_DAYS
    assert apl.license_reading({"plan": "trial", "is_expired": True}, None, T0).value == 0
    assert apl.license_reading(None, None, T0).value is None


def test_clock_and_clickhouse_readings():
    assert apl.time_reading({"ntp_synchronized": False, "ntp_service": "inactive"}).value == 1
    assert apl.time_reading({"ntp_synchronized": True}).value == 0
    assert apl.time_reading({"source": "unavailable"}).value is None
    assert apl.clickhouse_reading(None).value == 0
    down = apl.clickhouse_reading(ConnectionRefusedError("Connection refused to 127.0.0.1:8123"))
    assert down.value == 1 and down.facts == {"error": "connection"} and down.dimension == "clickhouse"


def test_error_text_never_carries_addresses_or_secrets():
    s = apl.scrub("550 rejected noc@example.com via smtp.mail.example.com 10.1.2.3 "
                  "fe80::1 token=abc password: hunter2 https://h.example/p?k=v +966 55 123 4567")
    for secret in ("noc@", "smtp.mail", "10.1.2.3", "fe80", "abc", "hunter2", "h.example", "123 4567"):
        assert secret not in s


def test_wording():
    disk = rule(apl.DISK, ">", 85)
    assert apl.sentence(disk, apl.Reading(91.0)) == "ZenPlus appliance: data disk at 91% (limit 85%)"
    assert apl.sentence(disk, apl.Reading(80.0), recovery=True) == \
        "ZenPlus appliance: data disk back to 80% (limit 85%)"
    chan = rule(apl.DELIVERY, ">=", 3)
    reading = apl.Reading(5.0, dimension=CH_A, label=apl.channel_label("NOC mail", "email"),
                          facts={"last_error": "connection: Connection refused"})
    assert apl.sentence(chan, reading) == ("ZenPlus appliance: email channel NOC mail failed 5 deliveries"
                                           " in the last hour (last error: connection: Connection refused)")
    assert apl.sentence(rule(apl.DB_DOWN), apl.clickhouse_reading(TimeoutError("timed out"))) == \
        "ZenPlus appliance: ClickHouse database is not answering (timeout)"
    assert apl.sentence(rule(apl.UPDATE), apl.Reading(1.0, facts={"version": "1.25.18", "status": "failed"})) \
        == "ZenPlus appliance: update to 1.25.18 failed"
    assert apl.sentence(rule(apl.TIME), apl.Reading(1.0, facts={"ntp_service": "inactive"})) == \
        "ZenPlus appliance: clock is not synchronised (NTP service inactive)"
    lic = apl.sentence(rule(apl.LICENSE, "<", 30), apl.Reading(12.4, facts={"expires_at": T0}))
    assert lic.startswith("ZenPlus appliance: licence expires in 12 days, on ") and lic.endswith("(limit 30 days)")


# ══ Migration ════════════════════════════════════════════════════════════════

SEEDED = {
    # name: (metric, operator, threshold, severity, priority, min_duration)
    "Appliance disk high": ("appliance_disk_pct", ">", 85, "warning", "P3", 600),
    "Appliance disk critical": ("appliance_disk_pct", ">", 95, "critical", "P2", 600),
    "Database down": ("appliance_db_down", ">", 0, "critical", "P1", 120),
    "Backup failed": ("appliance_backup_failed", ">", 0, "warning", "P3", 0),
    "Update failed": ("appliance_update_failed", ">", 0, "warning", "P3", 0),
    "Licence expiring": ("appliance_license_days", "<", 30, "warning", "P3", 0),
    "Licence expiring soon": ("appliance_license_days", "<", 7, "critical", "P2", 0),
    "Clock not synchronised": ("appliance_time_unsynced", ">", 0, "warning", "P3", 900),
    "Notification channel failing": ("appliance_delivery_failures", ">=", 3, "warning", "P3", 0),
    "Remote sensor offline": ("appliance_sensor_offline", ">", 0, "critical", "P2", 180),
}


def seeded(db):
    # The migrate-198 rules (migrate-206 seeds more of the family; see test_security_ops_alerts).
    rows = db.query("SELECT name, metric, operator, threshold, severity, priority, min_duration,"
                    " enabled, recovery_alert, notify_channels FROM alert_rules"
                    " WHERE metric = ANY($1::text[]) ORDER BY name", [v[0] for v in SEEDED.values()])
    return {r[0]: r[1:] for r in rows}


def test_migration_seeds_enabled_rules_routed_by_the_priority_policy(pg_db):
    rules = seeded(pg_db)
    assert set(rules) == set(SEEDED)
    for name, (metric, op, thr, sev, prio, hold) in SEEDED.items():
        got = rules[name]
        assert got[:6] == (metric, op, thr, sev, prio, hold), name
        assert got[6] is True and got[7] is True and list(got[8]) == []
    assert {v[0] for v in SEEDED.values()} <= set(apl.APPLIANCE_METRICS)
    with pytest.raises(Exception):
        pg_db.execute("INSERT INTO alert_rules (name, metric, operator, threshold, severity)"
                      " VALUES ('x', 'appliance_bogus', '>', 0, 'warning')")


def test_migration_retires_open_rule_less_sensor_rows_and_is_idempotent(pg_db):
    sid = pg_db.scalar("INSERT INTO sensors (name, status) VALUES ('edge-1', 'offline') RETURNING id")
    pg_db.execute("INSERT INTO alerts (sensor_id, status, severity, message, metadata) VALUES"
                  " ($1, 'active', 'critical', 'Remote sensor edge-1 is offline',"
                  " '{\"source\": \"sensor_offline\", \"dedupe\": \"sensor_offline\"}'),"
                  " ($1, 'active', 'warning', 'degraded', '{\"source\": \"sensor_degraded\","
                  " \"dedupe\": \"sensor_degraded\"}')", sid)
    pg_db.psql_file(MIGRATION)
    rows = dict(pg_db.query("SELECT metadata->>'dedupe', status || ':' || COALESCE(metadata->>'resolved_by','')"
                            " FROM alerts"))
    assert rows == {"sensor_offline": "resolved:migration", "sensor_degraded": "active:"}
    assert len(seeded(pg_db)) == len(SEEDED)        # nothing seeded twice


# ══ The evaluator on a disposable PostgreSQL ═════════════════════════════════

class FakeSources(apl.Sources):
    """Readings set by the test; a None entry is a failed source."""

    def __init__(self):
        self.values: dict[str, object] = {}

    def _get(self, metric):
        v = self.values.get(metric)
        if isinstance(v, Exception):
            raise v
        return list(v or [])

    async def disk(self, db, now):
        return self._get(apl.DISK)

    async def databases(self, db, now):
        return self._get(apl.DB_DOWN)

    async def backup(self, db, now):
        return self._get(apl.BACKUP)

    async def update(self, db, now):
        return self._get(apl.UPDATE)

    async def license(self, db, now):
        return self._get(apl.LICENSE)

    async def time_sync(self, db, now):
        return self._get(apl.TIME)


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
    sent: list[tuple[list, dict]] = []

    async def dispatch(db, channel_ids, ctx):
        sent.append((list(channel_ids), ctx))
        return len(channel_ids)

    monkeypatch.setattr(has, "dispatch_to_channels", dispatch)
    return NS(db=pg_db, sent=sent, src=FakeSources())


async def run(w, now):
    async with session(w.db) as s:
        return await apl.evaluate_appliance_rules(s, now=now, sources=w.src)


def rule_id(w, name):
    return str(w.db.scalar("SELECT id FROM alert_rules WHERE name = $1", name))


def incidents(w, name=None):
    sql = ("SELECT a.status, a.severity, a.priority, a.message, a.metadata, a.sensor_id, r.name"
           " FROM alerts a JOIN alert_rules r ON r.id = a.rule_id"
           " WHERE a.metadata->>'source' = 'appliance_health'")
    rows = w.db.query(sql + (" AND r.name = $1" if name else "") + " ORDER BY a.triggered_at, a.id",
                      *([name] if name else []))
    return [NS(status=r[0], severity=r[1], priority=r[2], message=r[3], meta=r[4], sensor_id=r[5],
               rule=r[6]) for r in rows]


def only(w, *names):
    """Keep only these seeded rules enabled."""
    w.db.execute("UPDATE alert_rules SET enabled = (name = ANY($1::text[]))"
                 " WHERE metric LIKE 'appliance\\_%'", list(names))


@pytest.mark.asyncio
async def test_disk_lifecycle_hold_trigger_clear_hold_recovery(world):
    w = world
    only(w, "Appliance disk high", "Appliance disk critical")
    w.src.values[apl.DISK] = [apl.Reading(91.0, label="data disk", facts={"mount": "/data"})]
    for t in range(0, 600, 60):
        await run(w, T0 + t)
    assert incidents(w) == [] and w.sent == []
    await run(w, T0 + 600)                       # the 10-minute hold has passed
    [inc] = incidents(w)
    assert (inc.rule, inc.status, inc.severity, inc.priority) == ("Appliance disk high", "active", "warning", "P3")
    assert inc.message == "ZenPlus appliance: data disk at 91% (limit 85%)"
    assert inc.meta["entity_name"] == "ZenPlus appliance" and inc.meta["notified"] is True
    [(channels, ctx)] = w.sent
    assert channels == []                        # no channels: the P3 default policy routes it
    assert ctx["delivery_event"] == "trigger" and ctx["alert_id"] and ctx["priority"] == "P3"
    assert ctx["body"] == "ZenPlus appliance: data disk at 91% (limit 85%)."
    assert ctx["hostname"] == "ZenPlus appliance"

    # Still high, then inside the 2-point band: one incident, nothing re-sent.
    await run(w, T0 + 660)
    w.src.values[apl.DISK] = [apl.Reading(84.0)]
    for t in range(720, 1500, 60):
        await run(w, T0 + t)
    assert len(incidents(w)) == 1 and len(w.sent) == 1
    # Missing data never resolves.
    w.src.values[apl.DISK] = [apl.Reading(None)]
    for t in range(1500, 2400, 60):
        await run(w, T0 + t)
    assert incidents(w)[0].status == "active"
    # Cleared for the 300 s clear hold: resolved with a recovery notice.
    w.src.values[apl.DISK] = [apl.Reading(70.0)]
    for t in range(2400, 2700, 60):
        await run(w, T0 + t)
    assert incidents(w)[0].status == "active"
    await run(w, T0 + 2700)
    [inc] = incidents(w)
    assert inc.status == "resolved" and inc.meta["resolved_by"] == "appliance_evaluator"
    assert len(w.sent) == 2
    recovery = w.sent[1][1]
    assert recovery["delivery_event"] == "recovery" and recovery["is_recovery"]
    assert recovery["alert_id"] == w.sent[0][1]["alert_id"]
    assert recovery["body"].startswith("ZenPlus appliance: data disk back to 70% (limit 85%).")


@pytest.mark.asyncio
async def test_a_failed_source_changes_nothing(world):
    w = world
    only(w, "Update failed")
    w.src.values[apl.UPDATE] = [apl.Reading(1.0, facts={"version": "1.25.18", "status": "failed"})]
    await run(w, T0)
    assert [i.status for i in incidents(w)] == ["active"]
    w.src.values[apl.UPDATE] = RuntimeError("history unreadable")
    for t in range(60, 1200, 60):
        await run(w, T0 + t)
    assert [i.status for i in incidents(w)] == ["active"] and len(w.sent) == 1


@pytest.mark.asyncio
async def test_a_hand_resolved_incident_is_not_re_paged_in_the_same_pass(world):
    w = world
    only(w, "Update failed")
    w.src.values[apl.UPDATE] = [apl.Reading(1.0, facts={"version": "1.25.18", "status": "failed"})]
    await run(w, T0)
    w.db.execute("UPDATE alerts SET status = 'resolved', resolved_at = now()")
    await run(w, T0 + 60)
    assert len(incidents(w)) == 1 and len(w.sent) == 1
    await run(w, T0 + 120)                       # still failing: a new incident after the hold
    assert [i.status for i in incidents(w)] == ["resolved", "active"]
    # ... whose page waits out the rule's 300 s cooldown, then goes out once.
    assert len(w.sent) == 1
    for t in range(180, 600, 60):
        await run(w, T0 + t)
    assert len(w.sent) == 2 and w.sent[1][1]["delivery_event"] == "trigger"


@pytest.mark.asyncio
async def test_channel_failures_page_per_channel_through_every_rule_channel(world):
    w = world
    only(w, "Notification channel failing")
    w.db.execute("INSERT INTO notification_channels (id, name, type, config) VALUES"
                 " ($1, 'NOC mail', 'email', '{}'), ($2, 'NOC SMS', 'sms', '{}')", CH_A, CH_B)
    rid = rule_id(w, "Notification channel failing")
    w.db.execute("UPDATE alert_rules SET notify_channels = $2::jsonb WHERE id = $1", rid, [CH_A, CH_B])
    for i in range(5):
        w.db.execute("INSERT INTO alert_deliveries (channel_id, channel_name, channel_type, kind, outcome,"
                     " error, attempted_at) VALUES ($1, 'NOC mail', 'email', 'trigger', 'failed', $2,"
                     " to_timestamp($3))", CH_A,
                     "connection: [Errno 111] Connection refused to smtp.example.com", T0 - 600 + i)
    w.db.execute("INSERT INTO alert_deliveries (channel_id, kind, outcome, attempted_at)"
                 " VALUES ($1, 'trigger', 'failed', to_timestamp($2))", CH_B, T0 - 7200)   # too old
    await run(w, T0)
    [inc] = incidents(w)
    assert inc.meta["dimension"] == CH_A
    assert inc.message == ("ZenPlus appliance: email channel NOC mail failed 5 deliveries in the last hour"
                           " (last error: connection: [Errno 111] Connection refused to <host>)")
    # The alert about a failing channel goes to every channel of the rule.
    [(channels, ctx)] = w.sent
    assert channels == [CH_A, CH_B] and ("Channel", "email channel NOC mail") in ctx["details"]

    # The channel is deleted: closed without a recovery notice.
    w.db.execute("DELETE FROM notification_channels WHERE id = $1", CH_A)
    await run(w, T0 + 60)
    [inc] = incidents(w)
    assert inc.status == "resolved" and inc.meta["resolved_reason"] == "the channel was deleted"
    assert len(w.sent) == 1
    assert w.db.scalar("SELECT count(*) FROM appliance_alert_state WHERE dimension = $1", CH_A) == 0


@pytest.mark.asyncio
async def test_sensor_offline_is_one_rule_incident_and_the_sweep_adds_no_row(world):
    from app.services import sensor_health_service as shs
    w = world
    only(w, "Remote sensor offline")
    sid = str(w.db.scalar("INSERT INTO sensors (name, status, last_heartbeat_at, offline_after_s)"
                          " VALUES ('edge-1', 'online', now() - interval '2 hours', 300) RETURNING id"))
    async with session(w.db) as s:
        assert (await shs.sweep_sensor_health(s))["offline"] == 1
    assert w.db.scalar("SELECT count(*) FROM alerts") == 0       # no rule-less row any more

    await run(w, T0)
    await run(w, T0 + 120)
    assert incidents(w) == []
    await run(w, T0 + 180)                       # the 3-minute hold
    [inc] = incidents(w)
    assert (inc.severity, inc.priority, str(inc.sensor_id)) == ("critical", "P2", sid)
    assert inc.message.startswith("Remote sensor edge-1 is offline: checks it runs are not being made")
    assert w.sent[0][1]["hostname"] == "Remote sensor edge-1"

    w.db.execute("UPDATE sensors SET status = 'online', last_heartbeat_at = now()")
    for t in range(240, 540, 60):
        await run(w, T0 + t)
    assert incidents(w)[0].status == "active"
    await run(w, T0 + 540)
    assert incidents(w)[0].status == "resolved" and w.sent[-1][1]["is_recovery"]

    # Disabled by an administrator while offline: closed, no notice.
    w.db.execute("UPDATE sensors SET status = 'offline'")
    for t in range(600, 900, 60):
        await run(w, T0 + t)
    assert incidents(w)[-1].status == "active"
    sent = len(w.sent)
    w.db.execute("UPDATE sensors SET status = 'disabled'")
    await run(w, T0 + 960)
    assert incidents(w)[-1].status == "resolved" and len(w.sent) == sent


@pytest.mark.asyncio
async def test_database_down_is_p1_per_database(world):
    w = world
    only(w, "Database down")
    w.src.values[apl.DB_DOWN] = [apl.clickhouse_reading(TimeoutError("timed out"))]
    await run(w, T0)
    await run(w, T0 + 120)
    [inc] = incidents(w)
    assert (inc.severity, inc.priority, inc.meta["dimension"]) == ("critical", "P1", "clickhouse")
    assert w.sent[0][1]["priority"] == "P1" and w.sent[0][1]["groupable"] is False


@pytest.mark.asyncio
async def test_real_sources_read_backups_licence_and_deliveries(world):
    """The database-backed sources against real tables (no file or process reads)."""
    w = world
    src = apl.Sources()
    w.db.execute("INSERT INTO system_settings (key, value) VALUES ('storage.backup_schedule',"
                 " '{\"enabled\": true, \"frequency\": \"daily\"}') ON CONFLICT (key) DO UPDATE"
                 " SET value = EXCLUDED.value, updated_at = to_timestamp($1)", T0 - 40 * 3600)
    w.db.execute("UPDATE system_settings SET updated_at = to_timestamp($1)"
                 " WHERE key = 'storage.backup_schedule'", T0 - 40 * 3600)
    w.db.execute("INSERT INTO storage_backups (id, created_by, kind, status, include_clickhouse, error,"
                 " created_at, finished_at) VALUES (gen_random_uuid(), 'schedule', 'config', 'failed', false,"
                 " 'pg_dump: could not connect to 10.0.0.5', to_timestamp($1), to_timestamp($1))", T0 - 3600)
    w.db.execute("INSERT INTO subscriptions (plan, status, started_at, expires_at, max_devices,"
                 " max_service_checks, max_users) VALUES ('trial', 'active', to_timestamp($1),"
                 " to_timestamp($2), 50, 20, 5)", T0 - 86400, T0 + 5 * 86400)
    async with session(w.db) as s:
        [backup] = await src.backup(s, T0)
        assert backup.value == 1 and backup.facts["reason"] == "failed"
        assert "10.0.0.5" not in backup.facts["error"]
        import app.api.v1.subscription as sub
        original = sub._load_remote_subscription
        sub._load_remote_subscription = lambda: None
        try:
            [lic] = await src.license(s, T0)
        finally:
            sub._load_remote_subscription = original
        assert lic.value == 5 and lic.facts["source"] == "local"
        assert await src.deliveries(s, T0) == []          # no channels configured
        assert await src.sensors(s, T0) == []


# ══ Rule API ═════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_rule_api_accepts_appliance_rules_and_drops_device_scope(world, monkeypatch):
    from app.api.v1 import alert_rules

    async def allow(*a, **k):
        return None

    monkeypatch.setattr(rs, "guard_rule_change", allow)
    monkeypatch.setattr(alert_rules, "write_audit_log", allow)
    user = NS(id=None, role="admin", scope_tags=[])
    async with session(world.db) as s:
        created = await alert_rules.create_alert_rule(alert_rules.AlertRuleCreate(
            name="Disk 80", metric="appliance_disk_pct", operator=">", threshold=80,
            scope_tag="lab", location="DC-1", clear_duration=600), s, user)
        assert created["family"] == "appliance"
        assert created["scope_tag"] is None and created["location"] is None
        updated = await alert_rules.update_alert_rule(created["id"], alert_rules.AlertRuleUpdate(
            conditions=[{"metric": "appliance_disk_pct", "operator": ">", "threshold": 85,
                         "reset_threshold": 75}], device_type="router"), s, user)
        assert updated["threshold"] == 85 and updated["device_type"] is None
        for bad in (
            [{"metric": "appliance_disk_pct", "operator": ">", "threshold": 85, "reset_threshold": 90}],
            [{"metric": "appliance_disk_pct", "operator": ">", "threshold": 85},
             {"metric": "cpu", "operator": ">", "threshold": 90}],
        ):
            with pytest.raises(HTTPException) as err:
                await alert_rules.create_alert_rule(alert_rules.AlertRuleCreate(
                    name="bad", metric="appliance_disk_pct", operator=">", threshold=85,
                    conditions=bad), s, user)
            assert err.value.status_code == 422


def test_scope_and_validator_patterns():
    from app.api.v1 import alert_rules
    for metric in apl.APPLIANCE_METRICS:
        alert_rules.AlertRuleCreate(name="x", metric=metric, operator=">", threshold=0)
        assert rs.family({"metric": metric}) == "appliance"
    with pytest.raises(Exception):
        alert_rules.AlertRuleCreate(name="x", metric="appliance_cpu", operator=">", threshold=0)
    assert rs.describe_scope({"metric": "appliance_disk_pct"})["label"] == "ZenPlus appliance"
    assert rs.describe_scope({"metric": "appliance_delivery_failures"})["label"] == "Every notification channel"
    from app.services import alert_rule_exclusions as rx
    assert rx.kinds_for_rule({"metric": "appliance_sensor_offline"}) == set()


def test_preview_names_the_appliance():
    v = apl.preview_variables({"metric": "appliance_db_down", "operator": ">", "threshold": 0,
                               "name": "Database down"}, is_recovery=False)
    assert v["hostname"] == "ZenPlus appliance"
    assert v["event_sentence"] == "ZenPlus appliance: ClickHouse database is not answering (connection)."
