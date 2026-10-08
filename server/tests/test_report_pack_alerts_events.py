"""Alert and event section pack (report_packs/alerts_events): KPIs, rows and
formatting from canned database rows, the organisation timezone, option
effects, NetFlow message redaction and device scoping."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.core.scoping import NO_MATCH_ID
from app.services import alert_workflow as wf
from app.services import report_sections as rs
from app.services import report_templates as rt
from app.services.branding import Branding
from app.services.netflow_alert_service import REDACTED_MESSAGE
from app.services.report_packs import alerts_events as pack

TO = datetime(2026, 10, 7, 12, 0)
FRM = TO - timedelta(days=7)
TS = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)  # 12:00 in Asia/Riyadh (UTC+3)
TARGETS = json.dumps({"critical": 10, "warning": 30})


# ── fakes ────────────────────────────────────────────────────────────────────

class Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = list(rows or [])
        self._scalar = scalar

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        return self._scalar


class FakeDB:
    """Answers each query with the canned result of the first route whose
    SQL substring matches; records every statement and its parameters."""

    def __init__(self, routes: dict, workflow: bool = True):
        self.routes = {"information_schema.columns": Result(scalar=workflow),
                       "FROM system_settings": Result([(TARGETS,)]), **routes}
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        for key, res in self.routes.items():
            if key in sql:
                return res
        return Result()

    def begin_nested(self):
        from tests.conftest import _NullTransaction
        return _NullTransaction()

    async def rollback(self):
        return None

    def params_for(self, fragment: str) -> dict:
        return next(p for s, p in self.calls if fragment in s)

    def sql_for(self, fragment: str) -> str:
        return next(s for s, _p in self.calls if fragment in s)


class _CHResult:
    def __init__(self, rows):
        self.result_rows = rows


class FakeCH:
    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None, **kw):
        self.calls.append((sql, dict(parameters or {})))
        for key, rows in self.routes.items():
            if key in sql:
                return _CHResult(list(rows))
        return _CHResult([])


@pytest.fixture(autouse=True)
def _fresh_workflow_cache(monkeypatch):
    monkeypatch.setattr(wf, "_SUPPORTED", None)


def run(db, sid, options=None, filters=None, ch=None, redact=False, monkeypatch=None,
        frm=FRM, to=TO):
    """Run one section function directly (exceptions surface in the test)."""
    if monkeypatch is not None:
        async def withholds(self):
            return redact
        monkeypatch.setattr(rs.SectionCtx, "withholds_netflow_alerts", withholds)
        if ch is not None:
            monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: ch)
    ctx = rs.SectionCtx(db, frm, to, filters)
    ctx.branding = Branding(timezone="Asia/Riyadh")
    ctx.section_id = sid
    ctx.section_options = rs.normalize_options(sid, options or {})
    return asyncio.run(rs.SECTION_REGISTRY[sid]["fn"](ctx))


def kpi(sec, label):
    return next(k for k in sec["kpis"] if k["label"] == label)


def table(sec, prefix):
    return next(t for t in sec["tables"] if (t.get("title") or "").startswith(prefix))


def well_formed(sec):
    for t in sec["tables"]:
        assert len(t["styles"]) == len(t["headers"])
        for row in t["rows"]:
            assert len(row) == len(t["headers"]), row
            assert all(isinstance(c, str) for c in row), row
    for k in sec["kpis"]:
        assert isinstance(k["value"], str)


# ── alert_response_metrics ──────────────────────────────────────────────────

def _response_db(workflow=True):
    sev = {"is_total": 0, "closed": 3, "acked": 3, "tta_p50": 300.0, "tta_p95": 900.0,
           "recovered": 2, "ttr_p50": 1800.0, "ttr_p95": 3600.0, "ttc_p50": 2000.0,
           "ttc_p95": 4000.0, "anomalies": 0}
    current = [
        {**sev, "severity": "warning", "opened": 6, "judged": 5, "breached": 1},
        {**sev, "severity": "critical", "opened": 4, "judged": 4, "breached": 1},
        {"severity": None, "is_total": 1, "opened": 10, "closed": 8, "acked": 6,
         "tta_p50": 420.0, "tta_p95": 1500.0, "recovered": 5, "ttr_p50": 2700.0,
         "ttr_p95": 7200.0, "ttc_p50": 3000.0, "ttc_p95": 9000.0, "judged": 9, "breached": 2,
         "anomalies": 1, "r_recovered": 5, "r_manual": 3, "r_suppressed": 0,
         "r_superseded": 0, "r_administrative": 0},
    ]
    prior = [{"severity": None, "is_total": 1, "opened": 8, "tta_p50": 600.0, "ttr_p50": 2700.0}]
    return FakeDB({
        "GROUPING SETS ((x.severity), ())": Result(current),
        "FROM x GROUP BY ()": Result(prior),
        "unassigned_critical": Result([{"ack_overdue": 2, "unassigned_critical": 1}]),
    }, workflow=workflow)


def test_response_metrics_kpis_table_and_closure_mix(monkeypatch):
    db = _response_db()
    sec = run(db, "alert_response_metrics", monkeypatch=monkeypatch)
    well_formed(sec)
    assert sec["title"] == "Alert Response Performance"
    assert kpi(sec, "Incidents opened")["value"] == "10"
    assert kpi(sec, "Incidents opened")["subtitle"] == "▲ 2 (+25.0%) vs previous period"
    tta = kpi(sec, "Time to acknowledge (median)")
    assert tta["value"] == "7.0 min"
    assert "p95 25 min" in tta["subtitle"] and "▼ 3.0 min vs previous period" in tta["subtitle"]
    ttr = kpi(sec, "Time to recover (median)")
    assert ttr["value"] == "45 min" and "No change vs previous period" in ttr["subtitle"]
    met = kpi(sec, "Acknowledged within target")
    assert met["value"] == "77.78%" and met["accent"] == "danger"
    assert met["subtitle"] == "2 of 9 breached the target"
    assert kpi(sec, "Acknowledgement overdue now")["value"] == "2"
    assert kpi(sec, "Unassigned critical now")["value"] == "1"

    rows = table(sec, "Response times by severity")["rows"]
    assert [r[0] for r in rows] == ["critical", "warning"]  # severity order, not row order
    assert rows[0] == ["critical", "4", "3", "5.0 min / 15 min", "30 min / 1.0 h",
                       "33 min / 1.1 h", "75.00%"]
    donut = sec["charts"][0]["series"]
    assert donut["kind"] == "donut"
    assert donut["points"] == [{"label": "Recovered automatically", "value": 5},
                               {"label": "Closed by operator", "value": 3}]
    assert any("critical 10 min, warning 30 min, info none" in n for n in sec["notes"])
    assert any("clock skew" in n for n in sec["notes"])
    # The configured targets reach the SQL; the window is explicit.
    p = db.params_for("GROUPING SETS ((x.severity), ())")
    assert (p["t_critical"], p["t_warning"], p["t_info"]) == (10, 30, None)
    assert (p["f"], p["t"]) == (FRM, TO)
    assert "NOT (COALESCE(a.metadata->>'is_recovery'" in db.sql_for("GROUPING SETS ((x.severity)")


def test_response_metrics_severity_option_and_missing_ownership(monkeypatch):
    db = _response_db(workflow=False)
    sec = run(db, "alert_response_metrics", {"severity": "critical"}, monkeypatch=monkeypatch)
    assert sec["title"] == "Alert Response Performance — Critical"
    assert db.params_for("GROUPING SETS ((x.severity), ())")["sev"] == ["critical"]
    assert db.params_for("unassigned_critical")["sev"] == ["critical"]
    assert kpi(sec, "Unassigned critical now")["value"] == "—"


def test_response_metrics_empty_period(monkeypatch):
    sec = run(FakeDB({}), "alert_response_metrics", monkeypatch=monkeypatch)
    assert kpi(sec, "Time to acknowledge (median)")["value"] == "—"
    assert kpi(sec, "Acknowledged within target")["value"] == "—"
    assert sec["notes"][0].startswith("No alert incidents")
    assert sec["tables"] == [] and sec["charts"] == []


# ── alert_backlog_aging ─────────────────────────────────────────────────────

def _backlog_db():
    z = {"open": 0, "unacked": 0, "suppressed": 0, "pending": 0, "unassigned": 0, "ack_overdue": 0}
    agg = [
        {**z, "severity": "critical", "bucket": "lt_1h", "open": 1, "unacked": 1, "unassigned": 1},
        {**z, "severity": "critical", "bucket": "gt_7d", "open": 2, "unacked": 1, "ack_overdue": 1},
        {**z, "severity": "warning", "bucket": "h1_24", "open": 3, "unacked": 2, "suppressed": 1,
         "unassigned": 2},
        {**z, "severity": "info", "bucket": "d1_7", "pending": 2},
    ]
    oldest = [
        {"severity": "critical", "message": "Traffic 9.2 Gbps towards 10.0.0.1 on uplink",
         "source_kind": "netflow", "source": "core-1", "status": "active",
         "triggered_at": TS - timedelta(days=8), "age_s": 8 * 86400.0, "owner": None},
        {"severity": "warning", "message": "Interface Gi0/1 down " + "x" * 200, "source_kind": None,
         "source": "edge-2", "status": "acknowledged", "triggered_at": TS,
         "age_s": 5400.0, "owner": "Sara Ali"},
    ]
    return FakeDB({"GROUP BY x.severity, x.bucket": Result(agg),
                   "AS age_s": Result(oldest)})


def test_backlog_aging_buckets_oldest_list_and_redaction(monkeypatch):
    db = _backlog_db()
    sec = run(db, "alert_backlog_aging", {"top_n": 2}, redact=True, monkeypatch=monkeypatch)
    well_formed(sec)
    assert kpi(sec, "Open incidents")["value"] == "6"
    assert kpi(sec, "Critical")["value"] == "3"
    assert kpi(sec, "Unacknowledged")["value"] == "4"
    assert kpi(sec, "Acknowledgement overdue")["value"] == "1"
    assert kpi(sec, "Open longer than 7 days")["value"] == "2"
    assert kpi(sec, "Unassigned")["value"] == "3"
    ages = table(sec, "Open incidents by age")
    assert ages["rows"] == [["critical", "1", "0", "0", "2", "3"],
                            ["warning", "0", "3", "0", "0", "3"]]  # info has nothing open
    assert sec["charts"][0]["series"]["points"][0] == {"label": "Under 1 hour", "value": 1}
    oldest = table(sec, "Oldest open incidents")
    assert oldest["title"] == "Oldest open incidents (top 2 of 6)"
    first, second = oldest["rows"]
    assert first[1] == REDACTED_MESSAGE  # NetFlow text withheld without netflow.view
    assert first[4] == "Sep 29 12:00" and first[5] == "8.0 d" and first[6] == "Unassigned"
    assert len(second[1]) == 120 and second[1].endswith("…")
    assert second[4] == "Oct 07 12:00" and second[5] == "1.5 h" and second[6] == "Sara Ali"
    assert db.params_for("AS age_s")["n"] == 2
    assert any("suppressed by a snooze" in n for n in sec["notes"])
    assert any("2 alert(s) are pending" in n for n in sec["notes"])


def test_backlog_shows_netflow_text_to_permitted_viewer(monkeypatch):
    sec = run(_backlog_db(), "alert_backlog_aging", redact=False, monkeypatch=monkeypatch)
    assert table(sec, "Oldest open incidents")["rows"][0][1].startswith("Traffic 9.2 Gbps")


def test_backlog_empty(monkeypatch):
    sec = run(FakeDB({}), "alert_backlog_aging", monkeypatch=monkeypatch)
    assert sec["notes"] == ["No open alert incidents when the report was generated."]


# ── alert_notification_delivery ─────────────────────────────────────────────

def _riyadh_midnight(day: int) -> int:
    return int(datetime(2026, 10, day, 21, 0, tzinfo=timezone.utc).timestamp())


def _delivery_db():
    channels = [
        {"channel": "PagerDuty", "ctype": "webhook", "is_total": 0, "attempts": 50, "accepted": 50,
         "failed": 0, "skipped": 0, "p95_ms": 400.0, "last_failed": None},
        {"channel": "NOC email", "ctype": "email", "is_total": 0, "attempts": 100, "accepted": 95,
         "failed": 3, "skipped": 2, "p95_ms": 1200.0, "last_failed": TS},
        {"channel": None, "ctype": None, "is_total": 1, "attempts": 150, "accepted": 145,
         "failed": 3, "skipped": 2, "p50_ms": 300.0, "p95_ms": 1100.0, "alerts_failed": 2},
    ]
    trend = [{"b": float(_riyadh_midnight(5)), "outcome": "accepted", "n": 10},
             {"b": float(_riyadh_midnight(5)), "outcome": "failed", "n": 1}]
    reasons = [{"category": "timeout", "n": 2, "channels": 1,
                "sample": "timeout: SMTP server timed out after 30 s", "last_at": TS},
               {"category": "other", "n": 1, "channels": 1, "sample": None, "last_at": TS}]
    return FakeDB({"GROUPING SETS ((x.channel, x.ctype), ())": Result(channels),
                   "dl.outcome, count(*) AS n": Result(trend),
                   "split_part(dl.error": Result(reasons)})


def test_notification_delivery_rates_latency_and_reasons(monkeypatch):
    db = _delivery_db()
    sec = run(db, "alert_notification_delivery", monkeypatch=monkeypatch)
    well_formed(sec)
    assert kpi(sec, "Notification attempts")["value"] == "150"
    delivered = kpi(sec, "Delivered")
    assert delivered["value"] == "97.97%" and delivered["accent"] == "warning"
    assert delivered["subtitle"] == "145 accepted of 148 sent"
    assert kpi(sec, "Failed")["subtitle"] == "2 alert(s) affected"
    assert kpi(sec, "Delivery latency (p95)")["value"] == "1.10 s"
    by_channel = table(sec, "Delivery by channel")
    assert by_channel["rows"][0] == ["NOC email", "email", "100", "95", "3", "2", "96.94%",
                                     "1.20 s", "Oct 07 12:00"]
    assert by_channel["rows"][1][-1] == "—"  # never failed
    reasons = table(sec, "Failure reasons")["rows"]
    assert reasons[0][:3] == ["Timed out", "2", "1"] and reasons[1][0] == "Unclassified"
    assert reasons[1][3] == "—"
    series = sec["charts"][0]["series"]
    assert series["kind"] == "stacked" and [s["name"] for s in series["series"]] == \
        ["Accepted", "Failed", "Skipped"]
    # Daily buckets start at local (Asia/Riyadh) midnight.
    assert series["points"][0]["t"] == "2026-09-29T21:00:00Z"
    day = next(p for p in series["points"] if p["t"] == "2026-10-05T21:00:00Z")
    assert day["v"] == 11
    p = db.params_for("dl.outcome, count(*) AS n")
    assert p["unit"] == "day" and p["org_tz"] == "Asia/Riyadh"


def test_notification_delivery_hourly_for_short_windows(monkeypatch):
    db = _delivery_db()
    sec = run(db, "alert_notification_delivery", monkeypatch=monkeypatch, frm=TO - timedelta(hours=24))
    assert db.params_for("dl.outcome, count(*) AS n")["unit"] == "hour"
    assert len(sec["charts"][0]["series"]["points"]) == 25


def test_notification_delivery_top_n_and_pre_migration(monkeypatch):
    sec = run(_delivery_db(), "alert_notification_delivery", {"top_n": 1}, monkeypatch=monkeypatch)
    assert table(sec, "Delivery by channel")["title"] == "Delivery by channel (top 1 of 2)"
    monkeypatch.setattr(wf, "_SUPPORTED", None)  # a positive answer is cached per process
    old = run(FakeDB({}, workflow=False), "alert_notification_delivery", monkeypatch=monkeypatch)
    assert "migration 135" in old["notes"][0] and not old["tables"]


# ── alert_escalations ───────────────────────────────────────────────────────

def _escalation_db():
    levels = [{"level": "2", "is_total": 0, "escalations": 1, "repeats": 0, "alerts": 1},
              {"level": "1", "is_total": 0, "escalations": 4, "repeats": 2, "alerts": 4},
              {"level": None, "is_total": 1, "escalations": 5, "repeats": 2, "alerts": 4}]
    names = [{"level": "1", "name": "NOC email", "n": 4},
             {"level": "2", "name": "On-call SMS", "n": 1}]
    by_rule = [
        {"rid": None, "is_total": 0, "rule_name": None, "alerts": 1, "escalations": 1,
         "repeats": 0, "max_level": 1, "acked": 1, "still_open": 0},
        {"rid": "r1", "is_total": 0, "rule_name": "Core CPU high", "alerts": 3, "escalations": 4,
         "repeats": 2, "max_level": 2, "acked": 2, "still_open": 1},
        {"rid": None, "is_total": 1, "rule_name": "Core CPU high", "alerts": 4, "escalations": 5,
         "repeats": 2, "max_level": 2, "acked": 3, "still_open": 1, "ack_after_s": 900.0},
    ]
    worst = [{"severity": "critical", "message": "Flow volume 12 Gbps from 10.9.9.9",
              "source_kind": "netflow", "source": "core-1", "status": "active", "triggered_at": TS,
              "max_level": 2, "pages": 3, "rule_name": "Core CPU high"}]
    return FakeDB({"GROUPING SETS ((x.level), ())": Result(levels),
                   "jsonb_array_elements_text": Result(names),
                   "GROUPING SETS ((y.rid), ())": Result(by_rule),
                   "AS pages": Result(worst)})


def test_escalations_levels_rules_and_redaction(monkeypatch):
    db = _escalation_db()
    sec = run(db, "alert_escalations", redact=True, monkeypatch=monkeypatch)
    well_formed(sec)
    assert kpi(sec, "Escalations")["value"] == "5"
    assert kpi(sec, "Repeat pages")["value"] == "2"
    assert kpi(sec, "Incidents escalated")["value"] == "4"
    assert kpi(sec, "Highest level reached")["value"] == "Level 2"
    acked = kpi(sec, "Acknowledged after escalation")
    assert acked["value"] == "75.00%" and acked["subtitle"] == "Median 15 min after the first escalation"
    assert table(sec, "Escalations by level")["rows"] == [
        ["Level 1", "4", "2", "4", "NOC email"], ["Level 2", "1", "0", "1", "On-call SMS"]]
    rules = table(sec, "Escalations by rule")["rows"]
    assert rules[0][0] == "Core CPU high" and rules[1][0] == "No rule (built-in status alert)"
    worst = table(sec, "Incidents that escalated furthest")["rows"][0]
    assert worst[1] == REDACTED_MESSAGE and worst[4] == "Level 2" and worst[-1] == "Oct 07 12:00"
    assert sec["charts"][0]["series"]["points"][0] == {"label": "Core CPU high", "value": 3}
    assert db.params_for("AS pages")["n"] == 10


def test_escalations_empty(monkeypatch):
    sec = run(FakeDB({}), "alert_escalations", monkeypatch=monkeypatch)
    assert sec["notes"] == ["No alerts were escalated in this period."]


# ── alert_rule_effectiveness ────────────────────────────────────────────────

def _rules_db():
    base = {"auto_recovered": 0, "short_lived": 0, "manual": 0, "still_open": 0, "median_s": None,
            "flapping": 0, "metric": "cpu", "snap_name": None, "enabled": True}
    rows = [
        {**base, "rid": "a", "rule_name": "Interface down", "incidents": 20, "auto_recovered": 18,
         "short_lived": 12, "manual": 1, "median_s": 120.0},
        {**base, "rid": "b", "rule_name": None, "enabled": None, "snap_name": "Old BGP rule",
         "incidents": 3},
        {**base, "rid": "c", "rule_name": "CPU", "enabled": False, "incidents": 2},
        {**base, "rid": "d", "rule_name": "Flappy link", "incidents": 6, "flapping": 2,
         "median_s": 7200.0},
        {**base, "rid": None, "rule_name": None, "enabled": None, "incidents": 5, "manual": 3},
    ]
    return FakeDB({"flap AS (": Result(rows),
                   "AS silent": Result([{"enabled": 10, "disabled": 1, "silent": 7}])})


def test_rule_effectiveness_verdicts_and_kpis(monkeypatch):
    db = _rules_db()
    sec = run(db, "alert_rule_effectiveness", monkeypatch=monkeypatch)
    well_formed(sec)
    rows = table(sec, "Alert rules")["rows"]
    verdicts = {r[0]: r[-1] for r in rows}
    assert verdicts == {"Interface down": "noisy", "Old BGP rule (deleted)": "deleted",
                        "CPU": "disabled", "Flappy link": "flapping",
                        "No rule (built-in status alerts)": "review"}
    assert [r[0] for r in rows][:2] == ["Interface down", "Flappy link"]  # most incidents first
    assert rows[0][3] == "12" and rows[0][5] == "2.0 min"
    assert kpi(sec, "Rules that fired")["value"] == "4"
    assert kpi(sec, "Rules that fired")["subtitle"] == "36 incident(s)"
    assert kpi(sec, "Short-lived (< 5 min)")["value"] == "33.33%"
    assert kpi(sec, "Flapping conditions")["value"] == "2"
    assert kpi(sec, "From disabled or deleted rules")["value"] == "5"
    assert kpi(sec, "Enabled rules that did not fire")["value"] == "7"
    assert db.params_for("AS silent")["fired"] == ["a", "b", "c", "d"]
    assert db.params_for("flap AS (")["org_tz"] == "Asia/Riyadh"


def test_rule_effectiveness_options(monkeypatch):
    db = _rules_db()
    sec = run(db, "alert_rule_effectiveness", {"top_n": 2, "sort": "flapping", "short_minutes": 10},
              monkeypatch=monkeypatch)
    t = table(sec, "Alert rules")
    assert t["title"] == "Alert rules (top 2 of 5)"
    assert t["rows"][0][0] == "Flappy link"
    assert "Short-lived (< 10 min)" in t["headers"]
    assert db.params_for("flap AS (")["short"] == 10


# ── alert_by_device ─────────────────────────────────────────────────────────

def _device_db():
    rows = [
        {"device_id": "dev-2", "total": 3, "critical": 0, "warning": 3, "info": 0, "open_now": 0,
         "active_critical": 0, "last_at": TS, "hostname": "edge-rtr", "ip": "10.0.0.2",
         "location": "DC2"},
        {"device_id": "dev-1", "total": 10, "critical": 4, "warning": 5, "info": 1, "open_now": 2,
         "active_critical": 1, "last_at": TS, "hostname": "core-sw", "ip": "10.0.0.1",
         "location": "DC1"},
        {"device_id": "dev-3", "total": 0, "critical": 0, "warning": 0, "info": 0, "open_now": 1,
         "active_critical": 0, "last_at": None, "hostname": None, "ip": "10.0.0.3", "location": "—"},
    ]
    return FakeDB({"AS active_critical": Result(rows), "BETWEEN :pf AND :pt": Result(scalar=10)})


def test_alert_by_device_counts_delta_and_device_filter(monkeypatch):
    db = _device_db()
    sec = run(db, "alert_by_device", filters={"device_ids": ["dev-1", "dev-2", "dev-3"]},
              monkeypatch=monkeypatch)
    well_formed(sec)
    total = kpi(sec, "Device alert incidents")
    assert total["value"] == "13" and total["subtitle"] == "▲ 3 (+30.0%) vs previous period"
    assert kpi(sec, "Devices with alerts")["value"] == "2"
    assert kpi(sec, "Open now")["value"] == "3"
    rows = table(sec, "Alerts per device")["rows"]
    assert rows[0] == ["core-sw", "10.0.0.1", "DC1", "4", "5", "1", "10", "2", "Oct 07 12:00"]
    assert rows[2][0] == "10.0.0.3" and rows[2][-1] == "—"
    # The device filter reaches both queries.
    for frag in ("AS active_critical", "BETWEEN :pf AND :pt"):
        assert db.params_for(frag)["dev_ids"] == ["dev-1", "dev-2", "dev-3"]
        assert "a.device_id::text = ANY(:dev_ids)" in db.sql_for(frag)
    points = sec["charts"][0]["series"]["points"]
    assert [p["label"] for p in points] == ["core-sw", "edge-rtr"]  # zero counts left out


def test_alert_by_device_sort_and_top_n(monkeypatch):
    sec = run(_device_db(), "alert_by_device", {"sort": "open", "top_n": 2}, monkeypatch=monkeypatch)
    t = table(sec, "Alerts per device")
    assert t["title"] == "Alerts per device (top 2 of 3)"
    assert [r[0] for r in t["rows"]] == ["core-sw", "10.0.0.3"]
    assert sec["charts"][0]["title"] == "Top devices by open alerts"


def test_alert_by_device_unscoped_runs_without_device_clause(monkeypatch):
    db = _device_db()
    run(db, "alert_by_device", monkeypatch=monkeypatch)
    assert "dev_ids" not in db.params_for("AS active_critical")


# ── snmp_trap_summary ───────────────────────────────────────────────────────

def _trap_ch():
    return FakeCH({
        "uniqExactIf(source_ip_text": [(100, 10, 30, 60, 5, 4, 12, 2)],
        "toUnixTimestamp(toDateTime(": [(_riyadh_midnight(1), "critical", 3),
                                        (_riyadh_midnight(1), "info", 7)],
        "GROUP BY trap_name, trap_oid": [("linkDown", "1.3.6.1.6.3.1.1.5.3", "warning", 20, 3, TS),
                                         ("", "1.3.6.1.4.1.9.9.41.2.0.1", "info", 5, 1, TS)],
        "GROUP BY did": [("dev-1", 50, 5, 10, 4, TS)],
        "GROUP BY source_ip_text": [("192.0.2.9", 5, 2, "coldStart", TS)],
    })


def test_trap_summary_unfiltered(monkeypatch):
    ch = _trap_ch()
    db = FakeDB({"FROM devices WHERE id::text = ANY(:ids)": Result([("dev-1", "core-sw", "10.0.0.1")])})
    sec = run(db, "snmp_trap_summary", ch=ch, monkeypatch=monkeypatch)
    well_formed(sec)
    assert kpi(sec, "Traps received")["value"] == "100"
    assert kpi(sec, "Traps received")["subtitle"] == "12 trap type(s)"
    assert kpi(sec, "Critical")["value"] == "10" and kpi(sec, "Critical")["accent"] == "danger"
    assert kpi(sec, "Unknown sources")["value"] == "2"
    kinds = table(sec, "Most frequent traps")
    assert kinds["title"] == "Most frequent traps (top 2 of 12)"
    assert kinds["rows"][0] == ["linkDown", "1.3.6.1.6.3.1.1.5.3", "warning", "20", "3", "Oct 07 12:00"]
    assert kinds["rows"][1][0] == "—"
    assert table(sec, "Devices sending")["rows"][0][:3] == ["core-sw", "10.0.0.1", "50"]
    assert table(sec, "Traps from unknown sources")["rows"][0][0] == "192.0.2.9"
    series = sec["charts"][0]["series"]
    assert series["points"][0]["t"] == "2026-09-29T21:00:00Z"
    oct2 = next(i for i, p in enumerate(series["points"]) if p["t"] == "2026-10-01T21:00:00Z")
    assert series["points"][oct2]["v"] == 10
    assert series["series"][0]["points"][oct2]["v"] == 3
    trend_sql, trend_params = next(c for c in ch.calls if "toUnixTimestamp(toDateTime(" in c[0])
    assert "INTERVAL 1 DAY" in trend_sql and trend_params["org_tz"] == "Asia/Riyadh"
    assert all("dev_ids" not in p for _s, p in ch.calls)


def test_trap_summary_device_filter_excludes_unknown_sources(monkeypatch):
    ch = _trap_ch()
    sec = run(FakeDB({}), "snmp_trap_summary", filters={"device_ids": [NO_MATCH_ID]}, ch=ch,
              monkeypatch=monkeypatch)
    for sql, params in ch.calls:
        assert "device_id IN %(dev_ids)s" in sql and params["dev_ids"] == [NO_MATCH_ID]
        # NO_MATCH_ID is the zero UUID: legacy zero-UUID rows must not match it.
        assert pack.TRAP_KNOWN_SQL in sql
    assert not any("GROUP BY source_ip_text" in s for s, _p in ch.calls)
    assert all(k["label"] != "Unknown sources" for k in sec["kpis"])


def test_trap_summary_discloses_retention(monkeypatch):
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    sec = run(FakeDB({}), "snmp_trap_summary", ch=FakeCH({}), monkeypatch=monkeypatch,
              frm=now - timedelta(days=60), to=now)
    assert sec["notes"][0] == "No SNMP traps were received in this period."
    assert any("kept for 30 days" in n for n in sec["notes"])


# ── syslog_summary ──────────────────────────────────────────────────────────

def _syslog_db():
    groups = [{"g_sev": 0, "severity": 2, "facility": None, "n": 5},
              {"g_sev": 0, "severity": 3, "facility": None, "n": 10},
              {"g_sev": 0, "severity": 4, "facility": None, "n": 20},
              {"g_sev": 0, "severity": 6, "facility": None, "n": 65},
              {"g_sev": 1, "severity": None, "facility": 23, "n": 80},
              {"g_sev": 1, "severity": None, "facility": 4, "n": 20}]
    sources = [{"device_id": "dev-1", "source_ip": None, "n": 90, "severe": 5, "errors": 10,
                "warnings": 15, "last_at": TS, "hostname": "core-sw", "ip": "10.0.0.1"},
               {"device_id": None, "source_ip": "192.0.2.50", "n": 10, "severe": 0, "errors": 0,
                "warnings": 5, "last_at": TS, "hostname": None, "ip": None}]
    trend = [{"b": float(_riyadh_midnight(3)), "cls": "severe", "n": 5}]
    apps = [{"app": "sshd", "n": 30, "worst": 2, "total_apps": 4}]
    latest = [{"received_at": TS, "severity": 2, "facility": 23, "application": "sshd",
               "message": "Failed password " + "y" * 300, "source": "core-sw"}]
    return FakeDB({"GROUPING SETS ((e.severity), (e.facility))": Result(groups),
                   "AS warnings": Result(sources), "AS cls": Result(trend),
                   "total_apps": Result(apps), "e.severity <= :lvl": Result(latest)})


def test_syslog_summary_unfiltered(monkeypatch):
    db = _syslog_db()
    sec = run(db, "syslog_summary", monkeypatch=monkeypatch)
    well_formed(sec)
    assert kpi(sec, "Syslog messages")["value"] == "100"
    assert kpi(sec, "Emergency to critical")["value"] == "5"
    assert kpi(sec, "Errors")["value"] == "10"
    assert kpi(sec, "Warnings")["value"] == "20"
    assert kpi(sec, "Devices reporting")["value"] == "1"
    assert kpi(sec, "Unmatched sources")["value"] == "1"
    sev_rows = table(sec, "Messages by severity")["rows"]
    assert sev_rows[0] == ["critical", "2 · Critical", "5", "5.00%"]
    assert sev_rows[-1] == ["info", "6 · Informational", "65", "65.00%"]
    sources = table(sec, "Busiest sources")["rows"]
    assert sources[1][:2] == ["192.0.2.50 (unmanaged)", "192.0.2.50"]
    assert table(sec, "Top applications")["title"] == "Top applications (top 1 of 4)"
    latest = table(sec, "Latest messages")
    assert latest["title"] == "Latest messages (critical or worse)"
    row = latest["rows"][0]
    assert row[:5] == ["Oct 07 12:00", "core-sw", "critical", "local7", "sshd"]
    assert len(row[5]) == 200 and row[5].endswith("…")
    donut = sec["charts"][1]["series"]
    assert donut["points"][0] == {"label": "local7", "value": 80}
    assert db.params_for("e.severity <= :lvl")["lvl"] == 2


def test_syslog_summary_device_filter_and_min_severity(monkeypatch):
    db = _syslog_db()
    sec = run(db, "syslog_summary", {"min_severity": "warning", "top_n": 5},
              filters={"device_ids": ["dev-1"]}, monkeypatch=monkeypatch)
    for sql, params in db.calls:
        if "network_events" in sql:
            assert params["dev_ids"] == ["dev-1"]
            assert "e.device_id::text = ANY(:dev_ids)" in sql
    assert db.params_for("e.severity <= :lvl")["lvl"] == 4
    assert db.params_for("e.severity <= :lvl")["n"] == 5
    assert table(sec, "Latest messages")["title"] == "Latest messages (warning or worse)"
    assert all(k["label"] != "Unmatched sources" for k in sec["kpis"])


def test_syslog_summary_empty(monkeypatch):
    sec = run(FakeDB({}), "syslog_summary", monkeypatch=monkeypatch)
    assert sec["notes"][0] == "No syslog messages were received in this period."


# ── registration and templates ──────────────────────────────────────────────

SECTIONS = {
    "alert_response_metrics": ("fleet", ("alerts.view",)),
    "alert_backlog_aging": ("fleet", ("alerts.view",)),
    "alert_notification_delivery": ("fleet", ("alerts.view",)),
    "alert_escalations": ("fleet", ("alerts.view",)),
    "alert_rule_effectiveness": ("fleet", ("alerts.view",)),
    "alert_by_device": ("device", ("alerts.view",)),
    "snmp_trap_summary": ("device", ("devices.view",)),
    "syslog_summary": ("device", ("devices.view",)),
}
TEMPLATES = ("alert_response_performance", "alert_open_backlog", "alert_notification_health",
             "alert_escalation_review", "alert_rule_noise_review", "alerts_per_device",
             "snmp_trap_report", "syslog_events_summary", "network_event_log_review",
             "monthly_incident_review")


def test_sections_registered_with_scope_and_permissions():
    for sid, (scope, requires) in SECTIONS.items():
        entry = rs.SECTION_REGISTRY[sid]
        assert entry["scope"] == scope and entry["requires"] == requires, sid
    assert rs.SECTION_REGISTRY["alert_backlog_aging"]["semantics"] == "snapshot"
    assert {rs.SECTION_REGISTRY[s]["category"] for s in ("snmp_trap_summary", "syslog_summary")} == {"Events"}


def test_templates_registered_and_valid():
    assert rt.validate_catalog() == []
    for key in TEMPLATES:
        t = rt.TEMPLATES[key]
        assert t.category in ("alerts", "security")
        assert key in rs.REPORT_PRESETS
    assert rt.TEMPLATES["alert_response_performance"].featured
    assert rt.TEMPLATES["alert_response_performance"].title == "Alert Response Performance (MTTA / MTTR)"
    assert "devices" in rt.catalog_entry(rt.TEMPLATES["alerts_per_device"])["filterable"]


def test_template_renders_through_build_sections(monkeypatch):
    """Template entries carry options and title overrides end to end."""
    db = _response_db()

    async def withholds(self):
        return False
    monkeypatch.setattr(rs.SectionCtx, "withholds_netflow_alerts", withholds)
    entries = [e for e in rt.TEMPLATES["alert_response_performance"].sections
               if e["id"] == "alert_response_metrics"]
    out = asyncio.run(rs.build_sections(db, entries, FRM, TO, None, requester=None,
                                        branding=Branding(timezone="Asia/Riyadh")))
    assert [s.get("error") for s in out] == [None, None]
    assert out[1]["title"] == "Critical Alert Response"
    assert out[1]["options"] == {"severity": "critical"}
