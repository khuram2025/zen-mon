"""Fixes to the built-in report sections (report_sections.py, netflow_report.py).

Every test runs on fakes (no database, no ClickHouse):

1. human timestamps use the organisation's time zone and format, chart points
   are aware UTC ('Z') and day buckets follow the organisation's midnight;
2. node availability reads the ping table that covers the window and its
   headline is sample-weighted; unmeasured is null;
3. alert counts exclude notices and MTTR is the Alert Center's recovery time;
4. server performance is sample-weighted, skips disabled servers, keeps nulls;
5. filesystem capacity applies the capacity filter, freshness, disabled servers;
6. server inventory is never cut silently;
7. APM services read the RED rollup for the window, bucket-aligned;
8. an SLO without data is "No data", never "100% / ON TRACK";
9. usage sections are clamped to span retention and say so;
10. RUM counts views once, keys sessions by app+env, collapses vitals per view
    and falls back to the rollup beyond raw retention;
11. ignored APM issues are not listed;
12. interface sections use effective speed and rank capacity by p95;
13. network performance shows latency/loss from the ping data;
14. list sections take a top_n option and never cut a list silently.

Retention (spans, RUM events, interface samples) is the table's configured
TTL read from system.tables once per render, with the documented default when
it cannot be read.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import netflow_report as nr
from app.services import report_sections as rs
from app.services.branding import Branding

UTC = timezone.utc
START = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
D1, D2, D3 = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
              "33333333-3333-4333-8333-333333333333")
RIYADH = Branding(timezone="Asia/Riyadh")


# ── fakes ────────────────────────────────────────────────────────────────────

class Res:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return self.rows[0][0] if self.rows else None

    def mappings(self):
        return self


class DB:
    """Postgres stand-in answering by SQL substring; records every call."""

    def __init__(self, *handlers):
        self.handlers = list(handlers)
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        sql, params = str(stmt), dict(params or {})
        self.calls.append((sql, params))
        for key, rows in self.handlers:
            if key in sql:
                return Res(rows(sql, params) if callable(rows) else rows)
        return Res()

    def sql(self, key: str) -> list[tuple[str, dict]]:
        return [c for c in self.calls if key in c[0]]


class CH:
    """ClickHouse stand-in answering by SQL substring; records every data call.

    Table-retention lookups (system.tables) are recorded apart in
    ``ttl_calls`` and answered from ``ttl`` ({table: engine_full}); a table
    without an entry has no readable TTL, so the section uses its default."""

    def __init__(self, *handlers, ttl=None):
        self.handlers = list(handlers)
        self.calls: list[tuple[str, dict]] = []
        self.ttl_calls: list[tuple[str, dict]] = []
        self.ttl = dict(ttl or {})

    def query(self, sql, parameters=None, **_kw):
        params = dict(parameters or {})
        if "system.tables" in sql:
            self.ttl_calls.append((sql, params))
            engine = self.ttl.get(params.get("ttl_table"))
            return SimpleNamespace(result_rows=[(engine,)] if engine else [])
        self.calls.append((sql, params))
        for key, rows in self.handlers:
            if key in sql:
                return SimpleNamespace(result_rows=list(rows(sql, params) if callable(rows) else rows))
        return SimpleNamespace(result_rows=[])

    def sql(self, key: str) -> list[tuple[str, dict]]:
        return [c for c in self.calls if key in c[0]]


def make_ctx(db=None, frm=START, to=None, *, branding=RIYADH, filters=None, options=None, sid=""):
    ctx = rs.SectionCtx(db if db is not None else DB(), frm, to or frm + timedelta(hours=24), filters)
    ctx.branding = branding
    ctx.section_id = sid
    ctx.section_options = rs.normalize_options(sid, options) if sid else {}
    return ctx


@pytest.fixture
def ch(monkeypatch):
    holder = SimpleNamespace(client=CH())
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: holder.client)
    return holder


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(UTC).replace(microsecond=0)


# ── 1. organisation time zone ────────────────────────────────────────────────

def test_longest_outages_start_is_in_the_organisation_timezone(monkeypatch):
    async def node_availability(ctx):
        return {"outage_list": [{"hostname": "core-1", "started_at": START, "duration_s": 600,
                                 "in_window_s": 600, "ongoing": False}]}
    monkeypatch.setattr(rs, "_node_availability", node_availability)
    sec = run(rs._sec_top_outages(make_ctx()))
    # 00:00 UTC is 03:00 in Riyadh; no UTC "%m-%d %H:%M" string any more.
    assert sec["tables"][0]["rows"] == [["core-1", "Sep 01 03:00", "10 min"]]


def test_recent_alerts_time_and_row_limit(monkeypatch):
    async def no_redaction(self):
        return False
    monkeypatch.setattr(rs.SectionCtx, "withholds_netflow_alerts", no_redaction)
    row = SimpleNamespace(_mapping={"severity": "critical", "message": "Down", "source": "edge",
                                    "status": "active", "triggered_at": START + timedelta(hours=12),
                                    "metadata": {"source": None}})
    db = DB(("FROM alerts a LEFT JOIN devices", [row]))
    sec = run(rs._sec_recent_alerts(make_ctx(db, sid="recent_alerts", options={"top_n": 7})))
    assert sec["tables"][0]["rows"][0][4] == "Sep 01 15:00"
    assert db.calls[-1][1]["lim"] == 7 and "LIMIT :lim" in db.calls[-1][0]
    assert sec["notes"] == []                                  # one alert in total: nothing cut


def test_recent_alerts_say_when_the_list_is_cut(monkeypatch):
    async def no_redaction(self):
        return False
    monkeypatch.setattr(rs.SectionCtx, "withholds_netflow_alerts", no_redaction)
    rows = [SimpleNamespace(_mapping={"severity": "warning", "message": f"m{i}", "source": "edge",
                                      "status": "resolved", "triggered_at": START, "total": 40,
                                      "metadata": {"source": None}}) for i in range(3)]
    db = DB(("FROM alerts a LEFT JOIN devices", lambda sql, p: rows[:p["lim"]]))
    sec = run(rs._sec_recent_alerts(make_ctx(db, sid="recent_alerts", options={"top_n": 3})))
    assert "COUNT(*) OVER ()" in db.calls[-1][0]
    assert sec["notes"][0].startswith("Showing 3 of 40 alerts (latest first)")


def test_ncm_times_have_no_utc_suffix():
    ctx = make_ctx(branding=Branding(timezone="Asia/Riyadh", date_format="DD/MM/YYYY"))
    assert rs._ncm_when(ctx, "2026-09-01T10:00:00+00:00") == "01/09/2026 13:00"
    assert rs._ncm_when(ctx, None) == "never"


def test_ncm_outcome_days_are_local_and_points_are_utc_z():
    day = datetime(2026, 8, 31, 21, 0, tzinfo=UTC)          # Riyadh midnight
    db = DB(("date_trunc('day'", [SimpleNamespace(day=day, ok=3, failed=1)]),
            ("failure_category", [SimpleNamespace(cat="auth_failed", n=1)]))
    sec = run(rs._sec_ncm_outcomes(make_ctx(db)))
    sql, params = db.sql("date_trunc('day'")[0]
    assert "AT TIME ZONE CAST(:tz AS text)" in sql and params["tz"] == "Asia/Riyadh"
    pts = sec["charts"][0]["series"]["points"]
    assert pts == [{"t": "2026-08-31T21:00:00Z", "v": 3}]
    assert all("png" not in c for c in sec["charts"])


def test_alert_trend_buckets_by_local_day_without_notices():
    db = DB(("date_trunc(", [(datetime(2026, 8, 31, 21, tzinfo=UTC), 5, 2)]))
    sec = run(rs._sec_alert_trend(make_ctx(db, to=START + timedelta(days=7))))
    sql, params = db.calls[-1]
    assert "date_trunc('day'" in sql and params["tz"] == "Asia/Riyadh"
    assert "is_recovery" in sql                                    # notices excluded
    points = sec["charts"][0]["series"]["points"]
    # Sep 01 03:00 → Sep 08 03:00 Riyadh touches eight local days, each starting
    # at local midnight (21:00 UTC); a day without alerts is a 0 bar, not a gap.
    assert [p["t"] for p in points] == [f"2026-{m:02d}-{d:02d}T21:00:00Z" for m, d in
                                        [(8, 31)] + [(9, d) for d in range(1, 8)]]
    assert [p["v"] for p in points] == [5, 0, 0, 0, 0, 0, 0, 0]
    assert "2 critical alert(s) in the window." in sec["notes"]
    hourly = DB()
    sec = run(rs._sec_alert_trend(make_ctx(hourly, to=START + timedelta(hours=24))))
    assert "date_trunc('hour'" in hourly.calls[-1][0]
    assert len(sec["charts"][0]["series"]["points"]) == 24
    assert all(p["v"] == 0 for p in sec["charts"][0]["series"]["points"])


def test_alert_trend_query_failure_is_no_chart_data_not_zeroes():
    class Broken(DB):
        async def execute(self, stmt, params=None):
            raise RuntimeError("database unavailable")
    sec = run(rs._sec_alert_trend(make_ctx(Broken(), to=START + timedelta(days=7))))
    assert sec["charts"][0]["series"]["points"] == []


def test_local_bucket_starts_follow_dst_midnights():
    ny = Branding(timezone="America/New_York")
    # 2026-11-01 is the US fall-back day (25 hours long).
    frm = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    days = rs._local_bucket_starts(make_ctx(frm=frm, to=frm + timedelta(days=3), branding=ny), "day")
    assert [d.astimezone(ny.tz).strftime("%m-%d %H:%M") for d in days] == \
        ["10-31 00:00", "11-01 00:00", "11-02 00:00", "11-03 00:00"]
    assert days[2] - days[1] == timedelta(hours=25)


def test_netflow_labels_use_the_organisation_timezone():
    dt = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert nr._label_time(dt, RIYADH) == "2026-09-01 15:00 GMT+3"
    assert nr._label_time(dt) == "2026-09-01 12:00 UTC"            # no branding: explicit UTC
    assert nr._label_time(None, RIYADH) == "—"
    plan = SimpleNamespace(warnings=[("raw_retention", "Raw flows are kept 30 days")],
                           effective_from=dt, effective_to=dt + timedelta(hours=1))
    assert nr._plan_notes(plan, RIYADH) == [
        "Raw flows are kept 30 days (data from 2026-09-01 15:00 GMT+3 to 2026-09-01 16:00 GMT+3)."]
    ctx = make_ctx()
    assert nr._ctx_branding(ctx) is RIYADH and nr._ctx_tz(ctx) == "Asia/Riyadh"


def test_netflow_day_and_hour_buckets_start_at_local_midnight():
    frm = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)
    days = nr._bucket_starts(frm, frm + timedelta(days=3), 86400, "Asia/Riyadh")
    assert days[0] == datetime(2026, 8, 31, 21, 0, tzinfo=UTC)
    assert all(d.astimezone(RIYADH.tz).hour == 0 for d in days) and len(days) == 4
    two_h = nr._bucket_starts(frm, frm + timedelta(hours=6), 7200, "Asia/Kolkata")
    assert [t.astimezone(Branding(timezone="Asia/Kolkata").tz).strftime("%H:%M") for t in two_h] == \
        ["10:00", "12:00", "14:00", "16:00"]
    # Minute buckets and UTC keep the plain UTC floor.
    assert nr._bucket_starts(frm, frm + timedelta(minutes=10), 300, "Asia/Riyadh") == \
        [frm, frm + timedelta(minutes=5)]


def test_netflow_trend_series_buckets_in_the_report_timezone(monkeypatch):
    frm = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    to = frm + timedelta(days=30)
    calls = []

    async def plan_source(win, f, **kw):
        return SimpleNamespace(effective_from=frm, effective_to=to, empty=False,
                               warnings=[("raw_retention", "Older raw flows expired")],
                               from_sql=lambda cols, f, vis: ("(SELECT 1)", {}))

    async def run_query(sql, params, **kw):
        calls.append((sql, dict(params)))
        first_local_midnight = datetime(2026, 8, 31, 21, 0, tzinfo=UTC)
        if kw.get("label") == "report-trend":
            return SimpleNamespace(result_rows=[(first_local_midnight + timedelta(days=1), 864_000)])
        return SimpleNamespace(result_rows=[(first_local_midnight, 1)])

    monkeypatch.setattr(nr, "plan_source", plan_source)
    monkeypatch.setattr(nr, "run_query", run_query)
    monkeypatch.setattr(nr, "compile_filter", lambda f, table, vis: ("1", {}))
    ts, vals, notes = run(nr.trend_series(SimpleNamespace(), nr.FlowFilter(), None, "1 DAY",
                                          tz="Asia/Riyadh", branding=RIYADH))
    assert all("%(nf_trend_tz)s" in sql and p["nf_trend_tz"] == "Asia/Riyadh" for sql, p in calls)
    assert ts[0] == datetime(2026, 8, 31, 21, 0, tzinfo=UTC)
    assert vals[0] == 0.0 and vals[1] == pytest.approx(864_000 * 8 / 86400) and vals[2] is None
    assert "GMT+3" in notes[0] and "UTC" not in notes[0]


# ── 2. node availability: table by window, weighted headline ────────────────

def _availability_world(monkeypatch, samples):
    """samples: {device_id: (up, total)}; D3 has none (not measured)."""
    db = DB(("FROM devices", [(D1, "core-1", "10.0.0.1/32", "HQ", "up"),
                              (D2, "edge-1", "10.0.0.2/32", "DC", "up"),
                              (D3, "spare", "10.0.0.3/32", "", "unknown")]))
    client = CH(("GROUP BY did", [(d, up, total, 5.0, 9.0, total, 0.0) for d, (up, total) in samples.items()]
                 + [("44444444-4444-4444-8444-444444444444", 0, 1000, 1.0, 1.0, 1000, 0.0)]),
                ("GROUP BY b", [(START, sum(u for u, _ in samples.values()),
                                 sum(t for _, t in samples.values()))]))
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: client)
    monkeypatch.setattr(rs, "device_outage_episodes", lambda *a, **k: [])
    return db, client


@pytest.mark.parametrize("hours,table", [(2, "ping_metrics "), (72, "ping_metrics_5m"), (24 * 30, "ping_metrics_1h")])
def test_node_availability_reads_the_table_for_the_window(monkeypatch, hours, table):
    db, client = _availability_world(monkeypatch, {D1: (90, 100)})
    na = run(rs._node_availability(make_ctx(db, to=START + timedelta(hours=hours))))
    sql = client.sql("GROUP BY did")[0][0]
    assert f"FROM zenplus.{table.strip()} " in sql
    # Only current inventory is read (deleted devices never move the headline).
    assert client.sql("GROUP BY did")[0][1]["ids"] == [D1, D2, D3]
    assert na["table"] == table.strip()


def test_availability_headline_is_sample_weighted_and_unmeasured_is_null(monkeypatch):
    db, client = _availability_world(monkeypatch, {D1: (50, 100), D2: (1000, 1000)})
    ctx = make_ctx(db, sid="device_availability")
    kpis = run(rs._sec_availability_kpis(ctx))["kpis"]
    # (50 + 1000) / (100 + 1000) = 95.45 % -- not the 75 % mean of percentages,
    # and the deleted device's 0 % does not count.
    assert kpis[0]["value"] == "95.45%"
    rows = run(rs._sec_device_availability(ctx))["tables"][0]["rows"]
    by_node = {r[0]: r for r in rows}
    assert by_node["core-1"][4] == "50.00%" and by_node["edge-1"][4] == "100.00%"
    assert by_node["spare"][4] == "—" and by_node["spare"][5] == "—"     # not measured, not 0 %
    trend = run(rs._sec_availability_trend(ctx))
    pt = trend["charts"][0]["series"]["points"][0]
    assert pt["t"].endswith("Z") and pt["v"] == pytest.approx(95.45)
    assert "png" not in trend["charts"][0]
    bucket_sql, bucket_params = client.sql("GROUP BY b")[0]
    assert "%(org_tz)s" in bucket_sql and bucket_params["org_tz"] == "Asia/Riyadh"


def test_lowest_availability_devices_share_the_node_computation(monkeypatch):
    db, _client = _availability_world(monkeypatch, {D1: (50, 100), D2: (1000, 1000)})

    async def no_dataset(self, name):
        raise AssertionError("the technical dataset must not be needed")
    monkeypatch.setattr(rs.SectionCtx, "dataset", no_dataset)
    ctx = make_ctx(db, sid="device_uptime", options={"top_n": 1})
    rows = run(rs._sec_device_uptime(ctx))["tables"][0]["rows"]
    assert rows == [["core-1", "10.0.0.1", "50.00%", "0", "5 ms", "9 ms"]]


# ── 3. alert summary ─────────────────────────────────────────────────────────

def test_alert_kpis_exclude_notices_and_use_recovery_time(monkeypatch):
    from app.services import alert_service

    seen = {}

    async def summary(db, f, **kw):
        seen["f"] = f
        return {"window": {"time_to_recover": {"count": 3, "mean_s": 1800.0, "p50_s": 900.0}}}
    monkeypatch.setattr(alert_service, "summary", summary)
    db = DB(("FROM alerts a WHERE", [("critical", 2, 1), ("warning", 3, 4)]))
    sec = run(rs._sec_alert_kpis(make_ctx(db)))
    sql = db.calls[0][0]
    assert "FILTER (WHERE NOT" in sql and "is_recovery" in sql and "suppressed_by_dependency" in sql
    kpis = {k["label"]: k for k in sec["kpis"]}
    assert kpis["Total alerts"]["value"] == "5" and kpis["Critical"]["value"] == "2"
    assert kpis["MTTR"]["value"] == "30 min" and "3 incident" in kpis["MTTR"]["subtitle"]
    assert sec["notes"] == ["5 recovery or suppression notice(s) are not counted as alerts."]
    assert seen["f"].from_time.tzinfo is not None and seen["f"].to_time == START + timedelta(hours=24)


def test_alert_kpis_without_recoveries_show_no_mttr(monkeypatch):
    from app.services import alert_service

    async def summary(db, f, **kw):
        return {"window": {"time_to_recover": {"count": 0, "mean_s": None}}}
    monkeypatch.setattr(alert_service, "summary", summary)
    sec = run(rs._sec_alert_kpis(make_ctx(DB())))
    assert {k["label"]: k["value"] for k in sec["kpis"]}["MTTR"] == "—"


# ── 4. server performance ────────────────────────────────────────────────────

def test_server_performance_is_weighted_enabled_only_and_keeps_nulls(ch):
    db = DB(("FROM servers", [("s1", "web-1"), ("s2", "db-1"), ("s3", "idle-1")]))
    ch.client = CH(("host_cpu_metrics_5m", [("s1", 42.5, 90.0, 120), ("s3", None, None, 0)]),
                   ("host_memory_metrics_5m", [("s1", 60.0, 70.0, 120), ("s2", 55.0, 58.0, 12)]))
    sec = run(rs._sec_server_performance(make_ctx(db, sid="server_performance")))
    assert "<> 'disabled'" in db.calls[0][0]
    cpu_sql, cpu_params = ch.client.sql("host_cpu_metrics_5m")[0]
    assert "sum(avg_total_pct * sample_count)" in cpu_sql and cpu_params["ids"] == ["s1", "s2", "s3"]
    rows = sec["tables"][0]["rows"]
    assert rows[0] == ["web-1", "42.5%", "90.0%", "60.0%", "70.0%"]
    assert rows[1] == ["db-1", "—", "—", "55.0%", "58.0%"]            # memory only: CPU not measured
    assert any("1 enabled server(s) reported no CPU or memory samples" in n for n in sec["notes"])


# ── 5. filesystem capacity ───────────────────────────────────────────────────

def test_capacity_filesystems_filter_freshness_and_disabled():
    rows = [("web-1", "/", 95.0, 100 * 1024 ** 3, 5 * 1024 ** 3, True),
            ("web-1", "/data", 92.0, None, None, False),            # stale inventory
            ("db-1", "C:", 40.0, 50 * 1024 ** 3, 30 * 1024 ** 3, True)]
    db = DB(("server_filesystem_inventory", rows))
    sec = run(rs._sec_capacity_filesystems(make_ctx(db, sid="capacity_filesystems")))
    sql, params = db.calls[0]
    assert "lower(f.fs_type) NOT IN" in sql and "'udf'" in sql             # capacity filter
    assert "<> 'disabled'" in sql and params["fresh_h"] == rs.FS_FRESH_HOURS
    assert sec["kpis"][0]["value"] == "1"                                  # the stale 92 % is not counted
    assert [r[1] for r in sec["tables"][0]["rows"]] == ["/", "C:"]
    assert any("not reported by their agent" in n for n in sec["notes"])
    # Agents refresh every row at least every 10 minutes; an hour-old row is stale.
    assert rs.FS_FRESH_HOURS == 1


def test_capacity_filesystems_query_failure_is_not_measured():
    class Broken(DB):
        async def execute(self, stmt, params=None):
            raise RuntimeError("database unavailable")
    sec = run(rs._sec_capacity_filesystems(make_ctx(Broken(), sid="capacity_filesystems")))
    assert sec["kpis"][0]["value"] == "—" and sec["kpis"][0]["accent"] == "neutral"


# ── 6. server inventory ──────────────────────────────────────────────────────

def test_inventory_servers_discloses_its_row_limit():
    db = DB(("FROM servers", lambda sql, p: [(f"srv-{i}", "Linux", "6", "healthy", "prod", "agent", 250)
                                             for i in range(p["lim"])]))
    sec = run(rs._sec_inventory_servers(make_ctx(db, sid="inventory_servers")))
    assert db.calls[0][1]["lim"] == 100 and "LIMIT 50" not in db.calls[0][0]
    assert len(sec["tables"][0]["rows"]) == 100
    assert sec["notes"][0].startswith("Showing 100 of 250 servers")
    assert rs.SECTION_REGISTRY["inventory_servers"]["options"][0]["max"] == 1000


# ── 7. APM services ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("hours,table,minute", [(2, "apm_span_metrics_5m", None), (24 * 7, "apm_span_metrics_1h", 0)])
def test_apm_services_read_the_rollup_for_the_window(ch, hours, table, minute):
    frm = datetime(2026, 9, 1, 10, 7, tzinfo=UTC)
    ch.client = CH(("span_kind IN", [("checkout", 1000, 20, 80.0, 600.0, 900, 50),
                                     ("search", 500, 0, 20.0, 50.0, 500, 0)]))
    sec = run(rs._sec_apm_services(make_ctx(frm=frm, to=frm + timedelta(hours=hours), sid="apm_services",
                                            options={"top_n": 1})))
    sql, params = ch.client.calls[0]
    assert f"FROM zenplus.{table}" in sql and "timestamp < %(t)s" in sql
    assert params["f"] <= frm.replace(tzinfo=None) and params["f"].second == 0
    assert params["f"].minute % 5 == 0 and (minute is None or params["f"].minute == minute)
    assert {k["label"]: k["value"] for k in sec["kpis"]} == {
        "Services reporting": "2", "Healthy": "1", "Degraded / critical": "1"}
    assert len(sec["tables"][0]["rows"]) == 1 and sec["tables"][0]["rows"][0][-1] == "degraded"


def test_apm_services_six_hour_window_keeps_table_and_alignment_together(ch):
    # Exactly 6 h from an unaligned start: the 5-minute table (bucket-aligned
    # start), never the hourly table read from a 5-minute boundary, which
    # would drop the hour bucket the window starts in.
    frm = datetime(2026, 9, 1, 10, 7, tzinfo=UTC)
    ch.client = CH(("span_kind IN", []))
    run(rs._sec_apm_services(make_ctx(frm=frm, to=frm + timedelta(hours=6), sid="apm_services")))
    sql, params = ch.client.calls[0]
    assert "FROM zenplus.apm_span_metrics_5m" in sql and params["f"] == datetime(2026, 9, 1, 10, 5)


# ── 8. SLO attainment ────────────────────────────────────────────────────────

def test_slo_without_data_is_no_data_not_on_track(monkeypatch):
    from app.services import apm_slo_service

    slos = [{"name": "Checkout", "service_name": "checkout", "target": 99.9, "window_days": 30,
             "sli_type": "availability"},
            {"name": "Search", "service_name": "search", "target": 99.0, "window_days": 7,
             "sli_type": "availability"},
            {"name": "Custom", "service_name": "search", "target": 99.0, "window_days": 7,
             "sli_type": "custom"}]

    async def load(db):
        return slos

    def bad_fraction(service, env, op, sli, thr, window_s):
        return None if service == "checkout" else (0.005, 1000)       # half of a 1 % budget
    monkeypatch.setattr(apm_slo_service, "_load_slos", load)
    monkeypatch.setattr(apm_slo_service, "_bad_fraction", bad_fraction)
    rows = run(rs._sec_slo_attainment(make_ctx()))["tables"][0]["rows"]
    assert rows[0][3:] == ["—", "No data"]
    assert rows[1][3:] == ["50%", "ON TRACK"]
    assert rows[2][3:] == ["—", "Not evaluated"]


# ── 9. usage sections and span retention ─────────────────────────────────────

@pytest.mark.parametrize("fn", [rs._sec_usage_kpis, rs._sec_usage_pages, rs._sec_usage_users])
def test_usage_sections_clamp_to_span_retention(ch, fn):
    now = _now()
    ch.client = CH(("count(), uniqIf", [(10, 2, 3, 1, 120.0)]))
    sec = run(fn(make_ctx(frm=now - timedelta(days=30), to=now)))
    frm = ch.client.calls[0][1]["f"]
    reach = (now - timedelta(days=rs.SPAN_RETENTION_DAYS)).replace(tzinfo=None)
    assert abs((frm - reach).total_seconds()) < 120
    assert any("kept for 14 days" in n for n in sec["notes"])


def test_usage_clamp_follows_the_configured_span_retention(ch):
    now = _now()
    ch.client = CH(("count(), uniqIf", [(10, 2, 3, 1, 120.0)]),
                   ttl={"apm_spans": "MergeTree TTL toDateTime(timestamp) + toIntervalDay(7) SETTINGS x"})
    sec = run(rs._sec_usage_kpis(make_ctx(frm=now - timedelta(days=10), to=now)))
    assert ch.client.ttl_calls[0][1] == {"ttl_table": "apm_spans"}
    frm = ch.client.calls[0][1]["f"]
    assert abs((frm - (now - timedelta(days=7)).replace(tzinfo=None)).total_seconds()) < 120
    assert any("kept for 7 days" in n for n in sec["notes"])
    # A 5-day window is wholly retained: no clamp, no note.
    ch.client = CH(("count(), uniqIf", [(10, 2, 3, 1, 120.0)]), ttl=ch.client.ttl)
    sec = run(rs._sec_usage_kpis(make_ctx(frm=now - timedelta(days=5), to=now)))
    assert sec["notes"] == []


def test_usage_outside_span_retention_reads_nothing(ch):
    now = _now()
    sec = run(rs._sec_usage_kpis(make_ctx(frm=now - timedelta(days=40), to=now - timedelta(days=20))))
    assert ch.client.calls == []
    assert any("none of this period is retained" in n for n in sec["notes"])
    kpis = {k["label"]: k["value"] for k in sec["kpis"]}
    assert kpis["Error rate"] == "—" and kpis["P95 latency"] == "—"        # no requests: not 0 %


# ── 10. RUM ──────────────────────────────────────────────────────────────────

def test_rum_raw_counts_views_once_and_keys_sessions_by_app_env(ch):
    now = _now()
    ch.client = CH(("uniqIf(user_id", [(4, 2, 9, 3)]))
    sec = run(rs._sec_rum_audience(make_ctx(frm=now - timedelta(days=2), to=now)))
    sql = ch.client.calls[0][0]
    assert "apm_rum_events" in sql and "end_reason = 'view_start'" in sql
    assert "uniqExactIf((application_id, env, session_id), sampled = 1)" in sql
    assert "uniqExactIf((application_id, env, view_id)" in sql
    assert {k["label"]: k["value"] for k in sec["kpis"]}["Page views"] == "9"


def test_rum_vitals_collapse_each_view_to_one_final_sample(ch):
    now = _now()
    ch.client = CH(("quantileTDigestIf", [(2600.0, 10, 150.0, 8, 0.05, 10, 900.0, 10, 300.0, 10, 6, 3, 1)]))
    sec = run(rs._sec_rum_experience(make_ctx(frm=now - timedelta(days=1), to=now)))
    sql = ch.client.calls[0][0]
    assert "argMaxIf" in sql and "is_final = 1" in sql and "sampled = 1" in sql
    assert "GROUP BY application_id, env, view_id" in sql
    kpis = {k["label"]: k for k in sec["kpis"]}
    assert kpis["LCP (p75)"]["value"] == "2.60 s" and kpis["LCP (p75)"]["subtitle"] == "Needs work"
    assert sec["charts"][0]["series"]["points"][0] == {"label": "Good", "value": 6}


def test_rum_beyond_raw_retention_reads_the_rollup(ch):
    now = _now()
    ch.client = CH(("uniqCombined64Merge(sessions), uniqCombined64Merge(views)", [(40, 120, 30)]))
    ctx = make_ctx(frm=now - timedelta(days=30), to=now)
    sec = run(rs._sec_rum_audience(ctx))
    assert all("apm_rum_metrics_5m" in sql for sql, _ in ch.client.calls)
    kpis = {k["label"]: k for k in sec["kpis"]}
    assert kpis["Sessions"]["value"] == "40" and kpis["Page views"]["value"] == "120"
    assert kpis["Unique users"]["value"] == "—"                            # not in the rollup
    assert any("5-minute aggregates" in n for n in sec["notes"])
    run(rs._sec_rum_top_pages(ctx))
    run(rs._sec_rum_experience(ctx))
    assert not any("apm_rum_events" in sql for sql, _ in ch.client.calls)


def test_rum_raw_retention_follows_the_rum_settings_ttl(ch):
    now = _now()
    ttl = {"apm_rum_events": "MergeTree ORDER BY x TTL toDateTime(timestamp) + toIntervalDay(7)"}
    ch.client = CH(("uniqCombined64Merge(sessions), uniqCombined64Merge(views)", [(40, 120, 30)]), ttl=ttl)
    sec = run(rs._sec_rum_audience(make_ctx(frm=now - timedelta(days=10), to=now)))
    # 10 days is within the default 14 but beyond the configured 7: the rollup.
    assert ch.client.calls and all("apm_rum_metrics_5m" in sql for sql, _ in ch.client.calls)
    assert any("kept for 7 days" in n for n in sec["notes"])


def test_table_retention_is_read_once_per_render(ch):
    now = _now()
    ctx = make_ctx(frm=now - timedelta(days=2), to=now)
    for fn in (rs._sec_rum_audience, rs._sec_rum_top_pages, rs._sec_rum_experience, rs._sec_rum_reliability):
        run(fn(ctx))
    assert [p["ttl_table"] for _sql, p in ch.client.ttl_calls] == ["apm_rum_events"]
    assert run(rs._retention_days(ctx, "apm_rum_events", 99)) == rs._rum_raw_days()   # cached


def test_rum_reliability_beyond_raw_retention_is_not_measured(ch):
    now = _now()
    ch.client = CH(("sum(errors)", [(9, 4, 300)]))
    sec = run(rs._sec_rum_reliability(make_ctx(frm=now - timedelta(days=60), to=now - timedelta(days=30))))
    kpis = {k["label"]: k["value"] for k in sec["kpis"]}
    assert kpis["Errors"] == "9" and kpis["Error rate"] == "3.00%"
    assert kpis["Distinct issues"] == "—" and kpis["Users affected"] == "—"   # unknown, not 0
    assert not any("apm_rum_events" in sql for sql, _ in ch.client.calls)
    assert any("not available for this period" in n for n in sec["notes"])


def test_rum_reliability_last_seen_is_local(ch):
    now = _now()
    last = datetime(2026, 9, 1, 12, 0)                                   # naive UTC from ClickHouse
    ch.client = CH(("countIf(event_type = 'error'),", [(3, 1, 2, 1, 50)]),
                   ("GROUP BY error_fingerprint", [("TypeError: x", "TypeError", "/cart", 3, 2, last)]))
    sec = run(rs._sec_rum_reliability(make_ctx(frm=now - timedelta(days=1), to=now)))
    assert sec["tables"][0]["rows"][0][-1] == "Sep 01 15:00"
    assert {k["label"]: k["value"] for k in sec["kpis"]}["Error rate"] == "6.00%"


# ── 11. APM errors triage ────────────────────────────────────────────────────

def test_apm_errors_leave_out_ignored_issues(ch):
    db = DB(("apm_error_issues", [("ignoredgroup0001",)]))
    ch.client = CH(("apm_exceptions", [
        (b"ignoredgroup0001", "ValueError", "noise", "api", 900, datetime(2026, 9, 1, 12)),
        (b"realgroup0000001", "KeyError", "missing key", "api", 12, datetime(2026, 9, 1, 13)),
    ]))
    sec = run(rs._sec_apm_errors(make_ctx(db, sid="apm_errors", options={"top_n": 5})))
    assert "LIMIT 6" in ch.client.calls[0][0]                              # top_n + ignored groups
    assert sec["tables"][0]["rows"] == [["KeyError", "missing key", "api", "12", "Sep 01 16:00"]]
    assert sec["notes"] == ["1 exception group(s) marked Ignored in APM → Errors are not listed."]


# ── 12. interfaces: effective speed, p95 capacity, rollup beyond raw ────────

def _interface_world(ch):
    #            did, idx, in_avg, out_avg, in_p95, out_p95, in_max, out_max, n
    ch.client = CH(("snmp_if_metrics", [
        (D1, 1, 400e6, 100e6, 600e6, 150e6, 900e6, 200e6, 100),    # 10 Gbps port: busiest, low util
        (D1, 2, 40e6, 10e6, 48e6, 12e6, 50e6, 20e6, 100),          # 50 Mbps circuit: nearly full
        (D2, 7, 5e6, 5e6, 6e6, 6e6, 9e6, 9e6, 100),                # unknown speed
    ]))
    return DB(("device_interfaces", [(D1, "core-1", 1, "Te1/1", 10_000_000_000, None),
                                     (D1, "core-1", 2, "Gi0/2", 1_000_000_000, 50_000_000),
                                     (D2, "edge-1", 7, "eth7", 0, None)]))


def test_busiest_interfaces_use_effective_speed_and_unknown_is_dash(ch):
    db = _interface_world(ch)
    now = _now()
    sec = run(rs._sec_interface_utilization(make_ctx(db, frm=now - timedelta(days=1), to=now)))
    rows = sec["tables"][0]["rows"]
    assert rows[0][0] == "core-1 · Te1/1" and rows[0][3] == "4.0%"
    assert rows[1][0] == "core-1 · Gi0/2" and rows[1][3] == "80.0%"      # configured 50 Mbps wins
    assert rows[2][3] == "—"
    assert "FROM zenplus.snmp_if_metrics\n" in ch.client.calls[0][0]


def test_capacity_ranks_by_p95_utilisation_and_reads_rollup_beyond_raw(ch):
    db = _interface_world(ch)
    now = _now()
    ctx = make_ctx(db, frm=now - timedelta(days=40), to=now, sid="capacity_interfaces")
    sec = run(rs._sec_capacity_interfaces(ctx))
    rows = sec["tables"][0]["rows"]
    assert [r[0] for r in rows] == ["core-1 · Gi0/2", "core-1 · Te1/1"]   # p95 96 % before 6 %
    assert rows[0][1] == "50.0 Mbps" and rows[0][2] == "96.0%"
    assert "snmp_if_metrics_5m" in ch.client.calls[0][0]
    assert any("1 interface(s) with traffic have no known speed" in n for n in sec["notes"])
    assert any("5-minute averages" in n for n in sec["notes"])


def test_interfaces_read_raw_while_the_configured_raw_retention_covers_the_window(ch):
    db = _interface_world(ch)
    ch.client.ttl = {"snmp_if_metrics": "MergeTree TTL toDateTime(timestamp) + INTERVAL 60 DAY"}
    now = _now()
    sec = run(rs._sec_capacity_interfaces(make_ctx(db, frm=now - timedelta(days=40), to=now)))
    assert "FROM zenplus.snmp_if_metrics\n" in ch.client.calls[0][0]
    assert not any("5-minute averages" in n for n in sec["notes"])


def test_interfaces_beyond_rollup_retention_say_so(ch):
    db = _interface_world(ch)
    now = _now()
    sec = run(rs._sec_interface_utilization(make_ctx(db, frm=now - timedelta(days=92), to=now)))
    assert any("Interface 5-minute history is kept for 90 days" in n for n in sec["notes"])


# ── 13. network performance ──────────────────────────────────────────────────

def test_network_performance_shows_latency_and_loss(monkeypatch):
    async def node_availability(ctx):
        return {"table": "ping_metrics_5m", "nodes": [
            {"hostname": "fast", "rtt": 2.0, "p95_rtt": 4.0, "loss_pct": 0.0, "avail": 100.0},
            {"hostname": "slow", "rtt": 80.0, "p95_rtt": 240.0, "loss_pct": 2.5, "avail": 99.5},
            {"hostname": "silent", "rtt": None, "p95_rtt": None, "loss_pct": None, "avail": None},
        ]}
    monkeypatch.setattr(rs, "_node_availability", node_availability)
    sec = run(rs._sec_network_performance(make_ctx()))
    assert sec["tables"][0]["headers"] == ["Device", "Avg RTT", "P95 RTT", "Packet loss", "Availability"]
    assert sec["tables"][0]["rows"][0] == ["slow", "80 ms", "240 ms", "2.50%", "99.50%"]
    assert len(sec["tables"][0]["rows"]) == 2                                # unmeasured is not ranked
    kpis = {k["label"]: k["value"] for k in sec["kpis"]}
    assert kpis == {"Devices measured": "2", "Worst p95 latency": "240 ms", "Devices with ≥1% loss": "1"}


def test_node_loss_is_measured_while_reachable(monkeypatch):
    db = DB(("FROM devices", [(D1, "core-1", "10.0.0.1", "", "up")]))
    # 5m rollup: 100 samples, 90 up; summed loss 0.1*100 = 10 = 10 down probes + 0 while up.
    client = CH(("GROUP BY did", lambda sql, p: [(D1, 90.0, 100.0, 5.0, 7.0, 90, 0.0 if "greatest" in sql else 1)]))
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: client)
    monkeypatch.setattr(rs, "device_outage_episodes", lambda *a, **k: [])
    na = run(rs._node_availability(make_ctx(db, to=START + timedelta(days=2))))
    assert "sum((1 - uptime_pct) * sample_count)" in client.sql("GROUP BY did")[0][0]
    assert na["nodes"][0]["loss_pct"] == 0.0 and na["nodes"][0]["avail"] == 90.0


# ── 14. top_n options ────────────────────────────────────────────────────────

LIST_SECTIONS = {"ncm_at_risk": 50, "device_uptime": 10, "top_outages": 15, "recent_alerts": 25, "noisy_sources": 10,
                 "interface_utilization": 10, "capacity_interfaces": 15, "server_performance": 15,
                 "capacity_filesystems": 20, "apm_errors": 12, "usage_pages": 15, "usage_users": 15,
                 "rum_top_pages": 15, "service_availability": 25, "device_availability": 200,
                 "inventory_servers": 100, "network_performance": 10, "apm_services": 25}


def test_list_sections_declare_top_n():
    for sid, default in LIST_SECTIONS.items():
        opts = {o["key"]: o for o in rs.SECTION_REGISTRY[sid]["options"]}
        assert opts["top_n"]["default"] == default, sid
        assert opts["top_n"]["type"] == "int" and opts["top_n"]["min"] == 1, sid


def test_builtin_rows_option_is_the_registry_top_n_schema():
    assert rs._rows_option(15, 100) == rs.top_n_option(15, 100)
    for sid in LIST_SECTIONS:
        opt = next(o for o in rs.SECTION_REGISTRY[sid]["options"] if o["key"] == "top_n")
        assert {k: v for k, v in opt.items() if k != "help"} == \
            {k: v for k, v in rs.top_n_option(opt["default"], opt["max"]).items() if k != "help"}, sid


def test_ncm_devices_needing_action_disclose_their_row_limit(monkeypatch):
    devices = [{"device_id": str(i), "hostname": f"sw{i:02d}", "ip": "10.0.0.1", "eligible": True,
                "backup_state": "stale", "validated_at": None, "next_due": None, "last_error": None,
                "support": {"message": ""}} for i in range(5)]

    async def ncm_devices(ctx):
        return devices
    monkeypatch.setattr(rs, "_ncm_devices", ncm_devices)
    sec = run(rs._sec_ncm_at_risk(make_ctx(sid="ncm_at_risk", options={"top_n": 2})))
    assert [r[0] for r in sec["tables"][0]["rows"]] == ["sw00", "sw01"]
    assert sec["notes"][0].startswith("Showing 2 of 5 devices needing action")


def test_top_n_option_reaches_the_section(monkeypatch):
    async def technical(self, name):
        return {"noisy_alerts": [{"hostname": f"h{i}", "sample_message": "m", "severity": "warning",
                                  "alert_count": 10 - i} for i in range(10)]}
    monkeypatch.setattr(rs.SectionCtx, "dataset", technical)
    db = DB()
    db.begin_nested = None
    out = run(rs.build_sections(db, [{"id": "noisy_sources", "options": {"top_n": 3}}],
                                START, START + timedelta(days=1), branding=RIYADH))
    assert len(out[0]["tables"][0]["rows"]) == 3 and out[0]["options"] == {"top_n": 3}


def test_device_availability_lists_up_to_top_n_and_says_so(monkeypatch):
    async def node_availability(ctx):
        return {"filtered": False, "nodes": [
            {"hostname": f"n{i}", "ip": "10.0.0.1", "location": "", "status": "up", "avail": 99.0,
             "downtime_min": 1.0, "outages": 0, "rtt": 1.0} for i in range(5)]}
    monkeypatch.setattr(rs, "_node_availability", node_availability)
    sec = run(rs._sec_device_availability(make_ctx(sid="device_availability", options={"top_n": 2})))
    assert len(sec["tables"][0]["rows"]) == 2
    assert sec["notes"][0].startswith("Showing 2 of 5 nodes")


# ── every built-in section survives "no data at all" ─────────────────────────

BUILTIN_SECTIONS = sorted(
    sid for sid, e in rs.SECTION_REGISTRY.items()
    if getattr(e["fn"], "__module__", "") == "app.services.report_sections" and e["category"] != "Traffic")


@pytest.mark.parametrize("sid", BUILTIN_SECTIONS)
def test_builtin_section_renders_without_data(sid, monkeypatch):
    from app.core import database
    from app.services import report_data_service
    from tests.conftest import _NullTransaction

    empty = CH()
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: empty)
    monkeypatch.setattr(database, "get_clickhouse_client", lambda: empty)
    monkeypatch.setattr(report_data_service, "get_ch_client", lambda: empty)
    monkeypatch.setattr(rs, "device_outage_episodes", lambda *a, **k: [])
    for name in ("build_executive", "build_technical", "build_business", "build_inventory"):
        async def _empty(*a, **k):
            return {}
        monkeypatch.setattr(rs, name, _empty)

    async def no_redaction(self):
        return False
    monkeypatch.setattr(rs.SectionCtx, "withholds_netflow_alerts", no_redaction)
    db = DB()
    db.begin_nested = _NullTransaction
    now = _now()
    out = run(rs.build_sections(db, [sid], now - timedelta(days=7), now, None, requester=None,
                                branding=RIYADH))
    sec = out[0]
    assert not sec.get("error"), f"{sid}: {sec['notes']}"
    for c in sec["charts"]:
        assert "png" not in c and c["series"]["kind"] in ("area", "line", "bars", "stacked", "rank", "donut")
        for p in c["series"]["points"]:
            assert "t" not in p or p["t"] is None or p["t"].endswith("Z"), (sid, p)
    for t in sec["tables"]:
        for row in t["rows"]:
            assert len(row) == len(t["headers"]), (sid, row)


# ── charts carry series only ─────────────────────────────────────────────────

def test_rewritten_sections_emit_series_without_png(ch, monkeypatch):
    now = _now()
    ch.client = CH(("count(), uniqIf", [(10, 2, 3, 1, 120.0)]),
                   ("AS b, count()", [(now - timedelta(hours=1), 4), (now, 6)]))
    sec = run(rs._sec_usage_kpis(make_ctx(frm=now - timedelta(days=1), to=now)))
    assert sec["charts"] and all("png" not in c and c["series"]["points"] for c in sec["charts"])
    assert all(p["t"].endswith("Z") for p in sec["charts"][0]["series"]["points"])
