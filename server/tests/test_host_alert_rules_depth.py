"""Server rules (alerting plan 2.7, 2.8, G-9, G-10, G-13 follow-up).

* Maintenance: a server in a maintenance window is not judged; its open
  incidents stay open and quiet.
* Dependencies: a server behind an unhealthy parent device is recorded with
  ``suppressed_by_dependency`` and not paged; the trigger goes out late if
  it still breaches once the parent is healthy.
* Cooldown: a re-opened incident inside the rule's cooldown waits (pending)
  and is sent once the cooldown is over if it still breaches (host and APM).
* Depth metrics: swap, disk latency (per disk), NIC errors (per NIC), clock
  skew, days until a filesystem is full (per mount), and stopped automatic
  services (per service).

Unit tests first, then the real host evaluator against a disposable
PostgreSQL with synthetic servers and a fake ClickHouse.
"""
from __future__ import annotations

import bisect
import contextlib
import math
from types import SimpleNamespace as NS

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import host_alert_service as has
from app.services import host_depth_metrics as hdm
from app.services.network_conditions import auto_reset_threshold, supported

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

T0 = 1_800_000_000.0   # synthetic epoch seconds
CHANNEL = "00000000-0000-0000-0000-00000000c0de"


# ══ Units ═══════════════════════════════════════════════════════════════════

def test_depth_metrics_are_registered_everywhere():
    for m in ("host_swap_pct", "host_disk_latency_ms", "host_nic_errors", "host_clock_skew_s",
              "host_fs_days_left"):
        assert supported(m), m
    assert {"host_swap_pct", "host_disk_latency_ms", "host_nic_errors"} <= has.AVERAGED_METRICS
    assert "host_clock_skew_s" in has.THRESHOLD_METRICS and "host_clock_skew_s" not in has.AVERAGED_METRICS
    assert has.SNAPSHOT_METRICS == {"host_fs_days_left", "host_auto_service_stopped"}
    assert auto_reset_threshold({"metric": "host_swap_pct", "operator": ">", "threshold": 80}) == 78
    assert auto_reset_threshold({"metric": "host_nic_errors", "operator": ">", "threshold": 5}) is None
    from app.services import alert_phrasing as ap
    assert ap.format_value("host_disk_latency_ms", 25.25) == "25.2 ms" or \
        ap.format_value("host_disk_latency_ms", 25.25) == "25.3 ms"
    assert ap.format_value("host_fs_days_left", 5) == "5 days"
    assert ap.format_value("host_clock_skew_s", 75) == "75 s"
    assert ap.format_value("host_nic_errors", 2.5) == "2.5 errors/s"
    assert ap.metric_noun("host_auto_service_stopped") == "Automatic service"


def test_each_dimension_has_its_own_dedupe_key():
    def rule(metric):
        return NS(id="r1", metric=metric)
    assert has._dedupe(rule("host_filesystem_pct"), "/data") == "rule:r1:mount:/data"
    assert has._dedupe(rule("host_fs_days_left"), "/data") == "rule:r1:mount:/data"
    assert has._dedupe(rule("host_disk_latency_ms"), "sda") == "rule:r1:disk:sda"
    assert has._dedupe(rule("host_nic_errors"), "eth0") == "rule:r1:nic:eth0"
    assert has._dedupe(rule("host_auto_service_stopped"), "Spooler") == "rule:r1:service:Spooler"
    assert has._dedupe(rule("host_swap_pct"), None) == "rule:r1"
    opened = {("s1", "rule:r1:disk:sda"): 1, ("s1", "rule:r1:disk:sdb"): 1, ("s2", "rule:r1:disk:sdc"): 1}
    assert has._dims_open(rule("host_disk_latency_ms"), "s1", opened) == {"sda", "sdb"}
    assert has._dimension_noun(rule("host_nic_errors"), "eth0") == "Interface eth0"
    assert has._dimension_noun(rule("host_disk_latency_ms"), "sda") == "Disk sda"


def test_days_until_full():
    assert hdm.days_left(1000 / 86400, 0, 10_000, 100, 2 * 86400) == pytest.approx(10.0)
    assert math.isinf(hdm.days_left(-5.0, 10, 100, 100, 2 * 86400))      # shrinking: not filling
    assert math.isinf(hdm.days_left(0.0, 10, 100, 100, 2 * 86400))
    assert hdm.days_left(1.0, 10, 100, 9, 2 * 86400) is None              # too few samples
    assert hdm.days_left(1.0, 10, 100, 100, 3600) is None                 # under a day of history
    rows = [("s1", "/", 1000 / 86400, 0, 10_000, 100, 2 * 86400, T0 - 60),
            ("s1", "/old", 1.0, 0, 10_000, 100, 2 * 86400, T0 - 86400),
            ("s2", "/", 1.0, 0, 10_000, 3, 600, T0)]
    out = hdm.fs_days_left_readings(rows, {}, 300, T0)
    assert out["s1"]["/"].value == pytest.approx(10.0) and out["s1"]["/"].state == "current"
    assert out["s1"]["/old"].value is None and out["s1"]["/old"].state == "no_data"
    assert out["s2"]["/"].value is None and out["s2"]["/"].state == "insufficient_history"


def test_automatic_service_readings():
    assert hdm.service_reading("running", None, None).value == 0.0
    assert hdm.service_reading("start_pending", None, None).value == 0.0
    assert hdm.service_reading("failed", None, None).value == 1.0       # systemd failed
    assert hdm.service_reading("stopped", 1067, "stopped").value == 1.0  # crashed
    assert hdm.service_reading("stopped", 0, "stopped").value == 0.0     # finished cleanly
    assert hdm.service_reading("stopped", 1077, "stopped").value == 0.0  # never started since boot
    # No recorded stop to read: unknown, never healthy and never a breach.
    assert hdm.service_reading("stopped", None, None).value is None
    assert hdm.service_reading("stopped", 1067, "running").value is None
    pats = hdm.exclude_patterns("gupdate*, EdgeUpdate*;  sppsvc")
    assert pats == ["gupdate*", "edgeupdate*", "sppsvc"]
    assert hdm.excluded("gupdatem", None, pats) and hdm.excluded("x", "EdgeUpdate Service", pats)
    assert not hdm.excluded("Spooler", "Print Spooler", pats)


def test_automatic_service_snapshot_freshness_and_exclusions():
    latest = {"s1": T0 - 60, "s2": T0 - 7200}
    rows = [("s1", "Spooler", "Print Spooler", "stopped"), ("s1", "gupdate", "Google Update", "stopped"),
            ("s1", "W32Time", "Windows Time", "running"), ("s2", "Spooler", "Print Spooler", "stopped")]
    pats = hdm.exclude_patterns("gupdate*")
    assert hdm.stopped_candidates(rows, pats) == {("s1", "Spooler"), ("s2", "Spooler")}
    codes = {("s1", "Spooler"): (1067, "stopped")}
    out = hdm.auto_service_readings(latest, rows, codes, patterns=pats, freshness={}, default_fresh=300, now=T0)
    assert out["s2"] is None                                   # a 2-hour-old snapshot is not judged
    assert out["s1"]["Spooler"].value == 1.0 and out["s1"]["Spooler"].context["exit_code"] == 1067
    assert out["s1"]["gupdate"].state == hdm.GONE
    assert out["s1"]["W32Time"].value == 0.0


def test_clickhouse_queries_read_the_collected_columns(monkeypatch):
    import uuid
    from app.core import database
    calls = []

    class Client:
        def query(self, sql, parameters=None):
            calls.append((" ".join(sql.split()), parameters))
            return NS(result_rows=[])

    monkeypatch.setattr(database, "get_clickhouse_client", lambda: Client())
    for metric, column in ((hdm.SWAP, "swap_used_bytes"), (hdm.DISK_LATENCY, "avg_read_ms"),
                           (hdm.NIC_ERRORS, "rx_errors_ps")):
        assert has.fetch_host_history(metric, T0 - 900, T0) == {}
        assert column in calls[-1][0]
    sid = str(uuid.uuid4())
    assert hdm.fetch_exit_codes({(sid, "Spooler")}) == {}
    sql, params = calls[-1]
    assert "host_service_state" in sql and "argMax(exit_code, timestamp)" in sql
    assert params["sids"] == (uuid.UUID(sid),) and params["names"] == ("Spooler",)
    assert hdm.fetch_exit_codes(set()) == {} and len(calls) == 4      # nothing to ask
    hdm.fetch_fs_forecast(T0)
    assert "simpleLinearRegression" in calls[-1][0] and "host_filesystem_metrics" in calls[-1][0]


def test_host_rule_api_shapes_the_new_metrics():
    from app.api.v1.host_alert_rules import _METRIC, _auto_reset, _metric_rule
    import re
    for m in ("host_swap_pct", "host_disk_latency_ms", "host_nic_errors", "host_clock_skew_s",
              "host_fs_days_left", "host_auto_service_stopped"):
        assert re.fullmatch(_METRIC, m)
    assert _metric_rule("host_auto_service_stopped", "eq", 5, None) == ("gt", 0.0)
    assert _metric_rule("host_fs_days_left", "lt", 7, None) == ("lt", 7)
    with pytest.raises(HTTPException):
        _metric_rule("host_fs_days_left", "gt", 7, None)
    with pytest.raises(HTTPException):
        _metric_rule("host_fs_days_left", "lt", 0, None)
    assert _auto_reset("host_fs_days_left", "lt", 7) == 8.0


# ══ The host evaluator on a disposable PostgreSQL ═══════════════════════════

@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


class History:
    def __init__(self):
        self.data: dict = {}

    def add(self, metric, sid, points, dim=""):
        self.data.setdefault(metric, {}).setdefault(sid, {}).setdefault(dim, []).extend(points)

    def fetch(self, metric, since, until):
        out = {}
        for sid, dims in self.data.get(metric, {}).items():
            for dim, pts in dims.items():
                stamps = [t for t, _ in pts]
                lo, hi = bisect.bisect_left(stamps, since), bisect.bisect_right(stamps, until)
                if hi > lo:
                    out.setdefault(sid, {})[dim] = pts[lo:hi]
        return out


@pytest.fixture
def world(pg_db, monkeypatch):
    # A fresh database per test; only the seeded rules are cleared.
    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules")
    server = str(pg_db.scalar(
        "INSERT INTO servers (display_name, hostname, collection_mode, status, os_type)"
        " VALUES ('synthetic-a', 'srv-synthetic-a', 'agent', 'healthy', 'linux') RETURNING id"))
    history = History()
    sent: list[dict] = []

    async def dispatch(db, channel_ids, ctx):
        sent.append(ctx)
        return len(channel_ids)

    monkeypatch.setattr(has, "fetch_host_history", history.fetch)
    monkeypatch.setattr(has, "dispatch_to_channels", dispatch)
    hdm.reset_forecast_cache()
    yield NS(db=pg_db, server=server, history=history, sent=sent)
    hdm.reset_forecast_cache()


def add_rule(w, metric="host_memory_pct", threshold=90, *, operator="gt", min_duration=0,
             clear_duration=0, recovery_alert=True, cooldown=0, target=None):
    return str(w.db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, min_duration,"
        " clear_duration, recovery_alert, notify_channels, cooldown, target)"
        " VALUES ($1, $2, $3, $4, 'warning', $5, $6, $7, $8::jsonb, $9, $10) RETURNING id",
        f"{metric} rule", metric, operator, threshold, min_duration, clear_duration,
        recovery_alert, [CHANNEL], cooldown, target))


async def run(w, now):
    async with session(w.db) as s:
        return await has.evaluate_host_rules(s, now=now)


def incidents(w, sid=None):
    return w.db.query("SELECT id, status, metadata FROM alerts WHERE server_id = $1"
                      " ORDER BY triggered_at, id", sid or w.server)


async def feed(w, metric, values, *, start=T0, step=60, dim=""):
    for i, value in enumerate(values):
        t = start + i * step
        if value is not None:
            w.history.add(metric, w.server, [(t, value)], dim)
        await run(w, t + 5)


# ── Maintenance (plan 2.7) ───────────────────────────────────────────────────

def server_window(w, sid=None):
    return w.db.scalar("INSERT INTO device_maintenance (scope_type, scope_server_id, starts_at, ends_at)"
                       " VALUES ('server', $1, now() - interval '5 minutes', now() + interval '1 hour')"
                       " RETURNING id", sid or w.server)


@pytest.mark.asyncio
async def test_a_server_in_maintenance_is_not_judged_and_its_open_incident_stays(world):
    w = world
    add_rule(w)
    window = server_window(w)
    await feed(w, "host_memory_pct", [96, 96])
    assert incidents(w) == [] and w.sent == []                 # nothing raised in the window
    w.db.execute("DELETE FROM device_maintenance WHERE id = $1", window)
    await feed(w, "host_memory_pct", [96], start=T0 + 120)
    [(_, status, _)] = incidents(w)
    assert status == "active" and len(w.sent) == 1
    # A new window while the incident is open: healthy readings neither
    # resolve it nor send a recovery until the window is over.
    window = server_window(w)
    await feed(w, "host_memory_pct", [50, 50, 50], start=T0 + 180)
    [(_, status, _)] = incidents(w)
    assert status == "active" and len(w.sent) == 1
    w.db.execute("DELETE FROM device_maintenance WHERE id = $1", window)
    await feed(w, "host_memory_pct", [50], start=T0 + 360)
    [(_, status, _)] = incidents(w)
    assert status == "resolved" and [c["is_recovery"] for c in w.sent] == [False, True]


@pytest.mark.asyncio
async def test_maintenance_of_the_linked_device_covers_service_rules(world):
    w = world
    dev = w.db.scalar("INSERT INTO devices (hostname, ip_address, status) VALUES ('srv-dev', '192.0.2.40',"
                      " 'maintenance') RETURNING id")
    w.db.execute("UPDATE servers SET device_id = $2 WHERE id = $1", w.server, dev)
    add_rule(w, "host_service_down", 0, target="nginx")
    w.db.execute("INSERT INTO server_service_inventory (server_id, service_name, start_mode, state)"
                 " VALUES ($1, 'nginx', 'auto', 'stopped')", w.server)
    await run(w, T0)
    assert incidents(w) == [] and w.sent == []
    w.db.execute("UPDATE devices SET status = 'up' WHERE id = $1", dev)
    await run(w, T0 + 60)
    assert [s for _, s, _ in incidents(w)] == ["active"] and len(w.sent) == 1


# ── Dependencies (G-13 follow-up) ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_server_behind_a_down_parent_is_recorded_not_paged(world):
    w = world
    parent = w.db.scalar("INSERT INTO devices (hostname, ip_address, status) VALUES ('core-sw', '192.0.2.1',"
                         " 'down') RETURNING id")
    dep = w.db.scalar("INSERT INTO topology_dependencies (parent_device_id, child_server_id) VALUES ($1, $2)"
                      " RETURNING id", parent, w.server)
    add_rule(w)
    await feed(w, "host_memory_pct", [96, 96])
    [(alert_id, status, meta)] = incidents(w)
    assert status == "active" and w.sent == []
    assert meta["suppressed_by_dependency"] is True and meta["notified"] is False
    assert meta["parent_hostname"] == "core-sw" and meta["parent_status"] == "down"
    assert meta["dependency_id"] == str(dep)
    # Parent back up, the server still breaching: the trigger goes out, once.
    w.db.execute("UPDATE devices SET status = 'up' WHERE id = $1", parent)
    await feed(w, "host_memory_pct", [96, 96], start=T0 + 120)
    [(_, _, meta)] = incidents(w)
    assert meta["notified"] is True and len(w.sent) == 1 and w.sent[0]["alert_id"] == str(alert_id)


@pytest.mark.asyncio
async def test_a_suppressed_incident_that_clears_sends_nothing(world):
    w = world
    parent = w.db.scalar("INSERT INTO devices (hostname, ip_address, status) VALUES ('core-sw', '192.0.2.1',"
                         " 'degraded') RETURNING id")
    w.db.execute("INSERT INTO topology_dependencies (parent_device_id, child_server_id) VALUES ($1, $2)",
                 parent, w.server)
    add_rule(w)
    await feed(w, "host_memory_pct", [96, 96, 50, 50, 50])
    [(_, status, meta)] = incidents(w)
    assert status == "resolved" and meta["suppressed_by_dependency"] is True and w.sent == []
    # A row that does not suppress alerts is ignored.
    w.db.execute("UPDATE topology_dependencies SET suppress_alerts = false")
    await feed(w, "host_memory_pct", [96, 96], start=T0 + 300)   # the 2-minute average
    assert [s for _, s, _ in incidents(w)] == ["resolved", "active"] and len(w.sent) == 1


# ── Cooldown (plan 2.8) ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_reopened_incident_waits_for_the_cooldown_then_pages(world):
    w = world
    add_rule(w, cooldown=600)
    await feed(w, "host_memory_pct", [96, 96, 50, 50])       # paged, then resolved (clear 0)
    assert [s for _, s, _ in incidents(w)] == ["resolved"]
    assert [c["is_recovery"] for c in w.sent] == [False, True]
    await feed(w, "host_memory_pct", [96, 96, 96], start=T0 + 240)
    first, (second_id, status, meta) = incidents(w)
    assert status == "active" and meta["notified"] is False and meta["cooldown_until"] == pytest.approx(T0 + 845)
    assert len(w.sent) == 2                                   # the re-breach waits
    # The cooldown (600 s since the first trigger at T0+5) runs out: sent once.
    await feed(w, "host_memory_pct", [96] * 8, start=T0 + 420)
    [_, (_, _, meta)] = incidents(w)
    assert meta["notified"] is True
    assert [c["is_recovery"] for c in w.sent] == [False, True, False]
    assert w.sent[2]["alert_id"] == str(second_id)


@pytest.mark.asyncio
async def test_a_flap_inside_the_cooldown_is_recorded_without_a_page(world):
    w = world
    add_rule(w, cooldown=600)
    await feed(w, "host_memory_pct", [96, 96, 50, 50, 96, 96, 50, 50])
    statuses = [s for _, s, _ in incidents(w)]
    assert statuses == ["resolved", "resolved"]
    # One trigger and its recovery: the second episode was never announced,
    # so it has no recovery either.
    assert [c["is_recovery"] for c in w.sent] == [False, True]
    assert incidents(w)[1][2]["notified"] is False


@pytest.mark.asyncio
async def test_no_cooldown_pages_every_reopened_incident(world):
    w = world
    add_rule(w, cooldown=0)
    await feed(w, "host_memory_pct", [96, 96, 50, 50, 96, 96])
    assert [c["is_recovery"] for c in w.sent] == [False, True, False]


# ── Depth metrics (G-9) ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_swap_rule_is_averaged_and_servers_without_swap_are_not_judged(world):
    w = world
    rule = add_rule(w, "host_swap_pct", 80, min_duration=180)
    await feed(w, "host_swap_pct", [40, 40, 40, 95])          # 3-minute average 58%: not yet
    assert incidents(w) == []
    await feed(w, "host_swap_pct", [95, 95], start=T0 + 240)
    [(_, status, meta)] = incidents(w)
    assert status == "active" and meta["dedupe"] == f"rule:{rule}"
    assert w.sent[0]["headline_metric"]["value"] == "95%"
    # No swap configured: the history row is NULL, which is missing data.
    for i in range(10):
        w.history.add("host_swap_pct", w.server, [(T0 + 360 + 60 * i, None)])
        await run(w, T0 + 365 + 60 * i)
    [(_, status, meta)] = incidents(w)
    assert status == "active" and meta["data_state"] == "no_data"


@pytest.mark.asyncio
async def test_disk_latency_is_per_disk_and_skips_windows_servers(world):
    w = world
    rule = add_rule(w, "host_disk_latency_ms", 50)
    win = str(w.db.scalar("INSERT INTO servers (display_name, hostname, collection_mode, status, os_type)"
                          " VALUES ('win', 'srv-win', 'agent', 'healthy', 'windows') RETURNING id"))
    w.history.add("host_disk_latency_ms", w.server, [(T0, 120), (T0 + 60, 120)], "sdb")
    w.history.add("host_disk_latency_ms", w.server, [(T0, 2), (T0 + 60, 2)], "sda")
    w.history.add("host_disk_latency_ms", win, [(T0, 900), (T0 + 60, 900)], "C:")
    await run(w, T0 + 65)
    [(_, status, meta)] = incidents(w)
    assert meta["dedupe"] == f"rule:{rule}:disk:sdb" and meta["disk"] == "sdb"
    assert incidents(w, win) == []
    ctx = w.sent[0]
    assert ctx["headline_metric"]["value"] == "120 ms"
    assert ("Disk", "sdb") in ctx["details"]


@pytest.mark.asyncio
async def test_nic_errors_are_per_interface(world):
    w = world
    rule = add_rule(w, "host_nic_errors", 5)
    w.history.add("host_nic_errors", w.server, [(T0, 12.5), (T0 + 60, 12.5)], "eth1")
    w.history.add("host_nic_errors", w.server, [(T0, 0), (T0 + 60, 0)], "eth0")
    await run(w, T0 + 65)
    [(_, _, meta)] = incidents(w)
    assert meta["dedupe"] == f"rule:{rule}:nic:eth1" and meta["nic"] == "eth1"
    assert "Interface eth1" in w.sent[0]["headline_metric"]["label"]


@pytest.mark.asyncio
async def test_clock_skew_reads_every_upload(world):
    w = world
    agent = w.db.scalar("INSERT INTO agents (server_id, agent_uid, hostname, status) VALUES ($1, 'uid-a',"
                        " 'srv-synthetic-a', 'online') RETURNING id", w.server)

    def upload(n, t, skew):
        w.db.execute("INSERT INTO agent_host_result_batches (agent_id, batch_id, server_id, payload_sha256,"
                     " sequence_start, sequence_end, clock_skew_s, completed_at)"
                     " VALUES ($1, $2, $3, 'x', 0, 0, $4, to_timestamp($5))", agent, f"b{n}", w.server, skew, t)

    add_rule(w, "host_clock_skew_s", 60, min_duration=120)
    for n, t in enumerate(range(int(T0), int(T0) + 120, 60)):
        upload(n, t, -95)                                     # agent clock 95 s behind
    await run(w, T0 + 65)
    assert incidents(w) == []                                 # hold not reached yet
    upload(10, T0 + 120, -96)
    await run(w, T0 + 125)
    [(_, status, meta)] = incidents(w)
    assert status == "active" and meta["value"] == 96
    assert w.sent[0]["headline_metric"]["value"] == "96 s"
    for n, t in enumerate(range(int(T0) + 180, int(T0) + 360, 60)):
        upload(20 + n, t, 1)
    await run(w, T0 + 305)
    [(_, status, _)] = incidents(w)
    assert status == "resolved"


@pytest.mark.asyncio
async def test_filesystem_days_left_with_holds(world, monkeypatch):
    w = world
    rule = add_rule(w, "host_fs_days_left", 7, operator="lt", clear_duration=0)
    rows: list = []
    monkeypatch.setattr(hdm, "fetch_fs_forecast", lambda now: list(rows))
    day = 86400
    # /data grows 1 GB a day with 5 GB free; / does not grow.
    rows[:] = [(w.server, "/data", 1e9 / day, 95e9, 100e9, 500, 3 * day, T0 - 30),
               (w.server, "/", -1.0, 10e9, 100e9, 500, 3 * day, T0 - 30)]
    await run(w, T0)
    [(_, status, meta)] = incidents(w)
    assert status == "active" and meta["dedupe"] == f"rule:{rule}:mount:/data"
    assert meta["value"] == 5.0 and meta["growth_bytes_per_day"] == 1_000_000_000
    assert w.sent[0]["headline_metric"]["value"] == "5 days"
    # The forecast is cached for 15 minutes; then 7.5 days (above 7, below the
    # 8-day clear level) keeps it open, and 20 days resolves it.
    rows[:] = [(w.server, "/data", 1e9 / day, 92.5e9, 100e9, 500, 3 * day, T0 + 900)]
    await run(w, T0 + 600)
    assert [s for _, s, _ in incidents(w)] == ["active"]
    await run(w, T0 + 960)
    assert [s for _, s, _ in incidents(w)] == ["active"]
    rows[:] = [(w.server, "/data", 1e9 / day, 80e9, 100e9, 500, 3 * day, T0 + 1800)]
    await run(w, T0 + 1860)
    [(_, status, _)] = incidents(w)
    assert status == "resolved" and [c["is_recovery"] for c in w.sent] == [False, True]


@pytest.mark.asyncio
async def test_filesystem_days_left_min_duration_is_a_hold(world, monkeypatch):
    w = world
    add_rule(w, "host_fs_days_left", 7, operator="lt", min_duration=3600)
    day = 86400
    monkeypatch.setattr(hdm, "fetch_fs_forecast", lambda now: [
        (w.server, "/data", 1e9 / day, 95e9, 100e9, 500, 3 * day, now - 30)])
    await run(w, T0)
    assert incidents(w) == []
    hdm.reset_forecast_cache()
    await run(w, T0 + 1800)
    assert incidents(w) == []
    hdm.reset_forecast_cache()
    await run(w, T0 + 3600)
    assert [s for _, s, _ in incidents(w)] == ["active"]


# ── Automatic services (G-10) ────────────────────────────────────────────────

def snapshot(w, services, sid=None):
    """One agent snapshot: every row written in one statement (one updated_at)."""
    sid = sid or w.server
    w.db.execute("DELETE FROM server_service_inventory WHERE server_id = $1", sid)
    for name, mode, state in services:
        w.db.execute("INSERT INTO server_service_inventory (server_id, service_name, display_name,"
                     " start_mode, state, updated_at) VALUES ($1, $2, $2, $3, $4, now())", sid, name, mode, state)


@pytest.mark.asyncio
async def test_an_automatic_service_that_crashed_pages_per_service(world, monkeypatch):
    w = world
    codes = {"Spooler": (1067, "stopped"), "sppsvc": (0, "stopped"), "MapsBroker": (1077, "stopped"),
             "gupdate": (1, "stopped")}
    monkeypatch.setattr(hdm, "fetch_exit_codes",
                        lambda pairs: {p: codes[p[1]] for p in pairs if p[1] in codes})
    rule = add_rule(w, "host_auto_service_stopped", 0, target="gupdate*")
    snapshot(w, [("Spooler", "auto", "stopped"), ("sppsvc", "auto", "stopped"),
                 ("MapsBroker", "auto", "stopped"), ("gupdate", "auto", "stopped"),
                 ("Fax", "manual", "stopped"), ("W32Time", "auto", "running")])
    now = w.db.scalar("SELECT extract(epoch FROM now())")
    await run(w, float(now))
    [(_, status, meta)] = incidents(w)
    assert status == "active" and meta["dedupe"] == f"rule:{rule}:service:Spooler"
    assert meta["service"] == "Spooler" and meta["exit_code"] == 1067
    assert "Spooler" in w.sent[0]["event_sentence"] or "Spooler" in w.sent[0]["subject"]
    # Running again: resolved with a recovery notice.
    snapshot(w, [("Spooler", "auto", "running"), ("W32Time", "auto", "running")])
    await run(w, float(now) + 60)
    [(_, status, _)] = incidents(w)
    assert status == "resolved" and [c["is_recovery"] for c in w.sent] == [False, True]


@pytest.mark.asyncio
async def test_a_service_no_longer_automatic_closes_quietly(world, monkeypatch):
    w = world
    monkeypatch.setattr(hdm, "fetch_exit_codes", lambda pairs: {p: (1066, "stopped") for p in pairs})
    add_rule(w, "host_auto_service_stopped", 0)
    snapshot(w, [("app", "auto", "stopped")])
    now = float(w.db.scalar("SELECT extract(epoch FROM now())"))
    await run(w, now)
    assert [s for _, s, _ in incidents(w)] == ["active"]
    snapshot(w, [("app", "disabled", "stopped")])          # the operator disabled it on purpose
    await run(w, now + 60)
    [(_, status, meta)] = incidents(w)
    assert status == "resolved" and meta["resolved_by"] == "not_watched"
    assert len(w.sent) == 1                                 # no "running again" notice


@pytest.mark.asyncio
async def test_an_old_service_snapshot_is_unknown(world, monkeypatch):
    w = world
    monkeypatch.setattr(hdm, "fetch_exit_codes", lambda pairs: {p: (1066, "stopped") for p in pairs})
    add_rule(w, "host_auto_service_stopped", 0)
    snapshot(w, [("app", "auto", "stopped")])
    now = float(w.db.scalar("SELECT extract(epoch FROM now())"))
    await run(w, now)
    w.db.execute("UPDATE server_service_inventory SET state = 'running', updated_at = now() - interval '3 hours'")
    await run(w, now + 60)
    [(_, status, meta)] = incidents(w)
    assert status == "active" and len(w.sent) == 1


# ── APM pull rules: cooldown (plan 2.8) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_apm_trigger_waits_for_the_cooldown(pg_db, monkeypatch):
    from app.services import apm_alert_service as apm
    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules")
    rule = pg_db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, notify_channels, cooldown,"
        " recovery_alert) VALUES ('p95', 'apm_latency_p95', '>', 800, 'warning', $1::jsonb, 600, true)"
        " RETURNING id", [CHANNEL])
    # The previous incident paged two minutes ago and has resolved.
    prev = pg_db.scalar(
        "INSERT INTO alerts (rule_id, status, severity, message, triggered_at, resolved_at, metadata)"
        " VALUES ($1, 'resolved', 'warning', 'p95', now() - interval '2 minutes', now(),"
        " $2::jsonb) RETURNING id", rule, {"service": "checkout", "notified": True})
    sent: list = []

    async def dispatch(db, channel_ids, ctx):
        sent.append(ctx)
        return 1

    monkeypatch.setattr(apm, "dispatch_to_channels", dispatch)
    fleet = {"checkout": {"apm_latency_p95": 950.0, "reqs": 100}}
    monkeypatch.setattr(apm, "_service_fleet", lambda w: fleet)
    async with session(pg_db) as s:
        await apm.evaluate_apm_rules(s)
    [(new_id, meta)] = pg_db.query("SELECT id, metadata FROM alerts WHERE status = 'active'")
    assert meta["notified"] is False and sent == []
    async with session(pg_db) as s:
        await apm.evaluate_apm_rules(s)                     # still inside the cooldown
    assert sent == []
    pg_db.execute("UPDATE alerts SET triggered_at = now() - interval '20 minutes' WHERE id = $1", prev)
    async with session(pg_db) as s:
        await apm.evaluate_apm_rules(s)
    [(_, meta)] = pg_db.query("SELECT id, metadata FROM alerts WHERE status = 'active'")
    assert meta["notified"] is True and len(sent) == 1 and sent[0]["alert_id"] == str(new_id)
