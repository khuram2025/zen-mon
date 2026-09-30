"""Availability report semantics (/reports/data/{executive,technical,business}).

Pins the contract the Availability page is built on: unmeasured is null, not
0; incidents are paired outage episodes, not alerts; durations come from the
gap between a down transition and its recovery (the status log's
duration_sec is always 0); active critical alerts do not depend on the range;
aggregates respect the caller's visibility scope and the current inventory.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import reports as reports_api
from app.core import scoping
from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app
from app.services import availability_policy
from app.services import report_data_service as rds
from app.services import report_service

UTC = timezone.utc
START = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
END = START + timedelta(hours=24)
D1, D2, GONE = str(uuid4()), str(uuid4()), str(uuid4())


def naive(ts):
    return ts.astimezone(UTC).replace(tzinfo=None)


# ── fakes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    """API client without the startup lifespan: no background loops, no database."""
    from tests.conftest import FakeDB

    async def fake_db():
        yield FakeDB()
    app.dependency_overrides[get_db] = fake_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


class Result:
    def __init__(self, rows=(), first=None):
        self.rows = list(rows)
        self._first = first

    def fetchall(self):
        return self.rows

    def all(self):
        return self.rows

    def first(self):
        return self._first

    def scalar(self):
        return self._first[0] if self._first else None


class ReportDB:
    """Answers the Postgres reads the report builders make, recording SQL."""

    def __init__(self, devices, alerts=(), active_critical=0, policy=None):
        self.devices, self.alerts = devices, list(alerts)
        self.active_critical, self.policy = active_critical, policy
        self.calls = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        if "FROM devices d" in sql and "device_groups" in sql:
            return Result([SimpleNamespace(_mapping=d) for d in self.devices])
        if "COUNT(*) FROM alerts" in sql:
            return Result(first=(self.active_critical,))
        if "FROM alerts a" in sql:
            rows = [a for a in self.alerts if params["start"] <= a["triggered_at"] <= params["end"]]
            return Result([SimpleNamespace(_mapping=a) for a in rows])
        if "system_settings" in sql:
            return Result(first=(self.policy,) if self.policy is not None else None)
        return Result()


class FakeCH:
    """ClickHouse stand-in: ping counts per window, status-log transitions."""

    def __init__(self, samples=None, transitions=(), honour_ids=True):
        # samples: {(start, end): {device_id: (up, total)}}; transitions: (did, ts, old, new)
        self.samples = samples or {}
        self.transitions = sorted(transitions, key=lambda t: t[1])
        self.honour_ids = honour_ids
        self.calls = []

    def query(self, sql, parameters=None):
        p = parameters or {}
        self.calls.append((sql, p))
        ids = p.get("ids")
        keep = (lambda did: did in ids) if (ids is not None and self.honour_ids) else (lambda did: True)
        if "device_status_log" in sql:
            rows = [t for t in self.transitions if keep(t[0])]
            if "timestamp < %(f)s" in sql:
                prior = {}
                for did, ts, old, new in rows:
                    if ts < p["f"]:
                        prior[did] = (did, naive(ts), old, new)
                return SimpleNamespace(result_rows=list(prior.values()))
            if "timestamp > %(t)s" in sql:
                out = {}
                for did, ts, old, new in rows:
                    if ts > p["t"] and new not in ("down", "offline") and did not in out:
                        out[did] = (did, naive(ts))
                return SimpleNamespace(result_rows=list(out.values()))
            return SimpleNamespace(result_rows=[(did, naive(ts), old, new) for did, ts, old, new in rows
                                                if p["f"] <= ts <= p["t"]])
        counts = self.samples.get((p.get("start"), p.get("end")), {})
        counts = {did: v for did, v in counts.items() if keep(did)}
        if "toStartOfInterval" in sql:
            up = sum(u for u, _ in counts.values())
            total = sum(t for _, t in counts.values())
            return SimpleNamespace(result_rows=[(naive(p["start"]), up, total)] if total else [])
        if "min(timestamp), max(timestamp), count()" in sql:
            n = sum(t for _, t in counts.values())
            return SimpleNamespace(result_rows=[(naive(p["start"]), naive(p["end"]), n)] if n else [(None, None, 0)])
        return SimpleNamespace(result_rows=[(did, u, t) for did, (u, t) in counts.items()])


def device(did, *, group="Core", dtype="router", location="HQ", status="up"):
    return {"id": did, "hostname": f"host-{did[:4]}", "ip_address": "10.0.0.1", "device_type": dtype,
            "location": location, "status": status, "last_seen": None, "last_rtt_ms": None,
            "ping_interval": 60, "group_name": group, "group_color": None}


def alert(did, at, severity="warning", status="resolved", resolved=None):
    return {"id": uuid4(), "device_id": did, "service_check_id": None, "status": status,
            "severity": severity, "message": "x", "triggered_at": at, "acknowledged_at": None,
            "resolved_at": resolved, "hostname": None}


def run_executive(monkeypatch, db, ch, start=START, end=END, visible_tags=None):
    monkeypatch.setattr(rds, "get_ch_client", lambda: ch)
    return asyncio.run(rds.build_executive(db, start, end, visible_tags=visible_tags))


# ── A01: not measured is null ───────────────────────────────────────────────

def test_no_samples_is_not_measured_not_zero(monkeypatch):
    db = ReportDB([device(D1), device(D2)])
    data = run_executive(monkeypatch, db, FakeCH())
    k = data["kpis"]
    assert k["availability_pct"] is None
    assert k["sla_attained_pct"] is None
    assert k["sla_met"] is None
    assert k["availability_delta_pp"] is None
    assert k["devices_expected"] == 2 and k["devices_measured"] == 0
    assert data["coverage"]["covered_pct"] is None
    assert data["coverage"]["measured_entities"] == 0
    # Every bucket is present and null: a gap, not an omitted or 0% point.
    assert data["availability_trend"] and all(p["availability_pct"] is None
                                              for p in data["availability_trend"])
    assert data["data_errors"] == []
    assert data["generated_at"]


def test_failed_subquery_reports_data_error(monkeypatch):
    class Broken(FakeCH):
        def query(self, sql, parameters=None):
            raise RuntimeError("secret host ch-internal:9000")
    data = run_executive(monkeypatch, ReportDB([device(D1)]), Broken())
    sources = {e["source"] for e in data["data_errors"]}
    assert {"device_availability", "device_status_log", "availability_trend"} <= sources
    assert all("ch-internal" not in e["message"] for e in data["data_errors"])
    assert data["kpis"]["availability_pct"] is None


def test_legacy_pdf_helpers_return_none_without_samples():
    assert report_service._device_uptime_pct([], D1) is None
    assert report_service._service_uptime_pct([], D1) is None


# ── A03: incidents are outage episodes; MTTR from recoveries ────────────────

def test_one_outage_with_many_alerts_is_one_incident(monkeypatch):
    down, up = START + timedelta(hours=2), START + timedelta(hours=2, minutes=30)
    alerts = [alert(D1, down + timedelta(minutes=i), resolved=down + timedelta(minutes=i + 1))
              for i in range(6)]
    ch = FakeCH(samples={(START, END): {D1: (95.0, 100.0)}},
                transitions=[(D1, START - timedelta(days=3), "unknown", "up"),
                             (D1, down, "up", "down"), (D1, up, "down", "up")])
    data = run_executive(monkeypatch, ReportDB([device(D1)], alerts), ch)
    k = data["kpis"]
    assert k["incidents_count"] == k["outage_episodes"] == 1
    assert k["alerts_count"] == 6
    assert k["mttr_minutes"] == pytest.approx(30)
    assert k["alert_mttr_minutes"] == pytest.approx(1)
    assert k["outage_downtime_minutes"] == pytest.approx(30)
    assert k["ongoing_outages"] == 0
    [row] = data["outage_timeline"]
    assert row["duration_minutes"] == pytest.approx(30)
    assert row["ended_at"] == up.isoformat() and row["ongoing"] is False
    [issue] = data["top_issues"]
    assert issue["category"] == "outage" and issue["outages"] == 1
    assert issue["downtime_minutes"] == pytest.approx(30)


def test_alert_only_device_is_alert_noise_not_incident(monkeypatch):
    ch = FakeCH(samples={(START, END): {D1: (100.0, 100.0)}})
    data = run_executive(monkeypatch, ReportDB([device(D1)], [alert(D1, START + timedelta(hours=1))]), ch)
    assert data["kpis"]["incidents_count"] == 0
    assert data["kpis"]["mttr_minutes"] is None
    assert data["top_issues"][0]["category"] == "alert_noise"


# ── A06: episode pairing, clipping, ongoing ─────────────────────────────────

def test_pairing_clips_to_window_and_keeps_ongoing():
    t = lambda h: START + timedelta(hours=h)  # noqa: E731
    episodes = {
        # Down before the window, recovered inside it.
        "a": rds.pair_outage_episodes([(naive(t(-2)), "up", "down"), (naive(t(1)), "down", "up")]),
        # Down inside, never recovered.
        "b": rds.pair_outage_episodes([(t(-5), "unknown", "up"), (t(20), "up", "down")]),
        # Down inside, recovered after the window ends.
        "c": rds.pair_outage_episodes([(t(23), "up", "down")], recovered_at=t(26)),
        # Log opens on a recovery: start unknown.
        "d": rds.pair_outage_episodes([(t(3), "down", "up")]),
        # Entirely before the window.
        "e": rds.pair_outage_episodes([(t(-4), "up", "down"), (t(-3), "down", "up")]),
    }
    now = t(30)
    got = {e["entity_id"]: e for e in rds.window_episodes(episodes, START, END, START, now=now)}
    assert set(got) == {"a", "b", "c", "d"}
    assert got["a"]["in_window_s"] == 3600 and got["a"]["duration_s"] == 3 * 3600
    assert got["a"]["completed_in_window"]
    assert got["b"]["ongoing"] and got["b"]["ended_at"] is None
    assert got["b"]["in_window_s"] == 4 * 3600 and got["b"]["duration_s"] == 10 * 3600
    assert not got["c"]["ongoing"] and not got["c"]["completed_in_window"]
    assert got["c"]["in_window_s"] == 3600 and got["c"]["duration_s"] == 3 * 3600
    assert got["d"]["started_at"] is None and got["d"]["duration_s"] is None
    assert got["d"]["in_window_s"] == 3 * 3600
    stats = rds._episode_stats(list(got.values()))
    assert stats["count"] == 4 and stats["ongoing"] == 1
    assert stats["mttr_s"] == 3 * 3600  # only "a" both started and recovered knowably in-window


def test_status_log_zero_duration_is_ignored(monkeypatch):
    """Down rows carry duration_sec=0; the recovery row defines the length."""
    down = START + timedelta(hours=5)
    ch = FakeCH(samples={(START, END): {D1: (99.0, 100.0)}},
                transitions=[(D1, down, "up", "down"), (D1, down + timedelta(seconds=31), "down", "up")])
    data = run_executive(monkeypatch, ReportDB([device(D1)]), ch)
    assert data["outage_timeline"][0]["duration_minutes"] == pytest.approx(31 / 60, abs=0.01)


def test_maintenance_excludes_planned_outage():
    t = lambda h: START + timedelta(hours=h)  # noqa: E731
    episodes = {"a": [(t(2), t(3))], "b": [(t(5), t(7))]}
    maint = {"a": [(naive(t(1)), naive(t(4)))], "b": [(naive(t(6)), naive(t(8)))]}
    got = {e["entity_id"]: e for e in rds.window_episodes(episodes, START, END, START, maint, now=END)}
    assert "a" not in got
    assert got["b"]["in_window_s"] == 3600  # the hour inside maintenance is not downtime


def test_service_episodes_from_confirmed_segments():
    t = lambda m: START + timedelta(minutes=m)  # noqa: E731
    segments = [(t(0), t(10), 1.), (t(10), t(15), 0.), (t(15), t(20), 0.), (t(20), t(30), 1.),
                (t(30), t(40), 0.), (t(45), t(50), 0.), (t(50), t(60), 0.)]
    end = t(60)
    eps = rds.confirmed_down_episodes(segments, end)
    assert eps == [(t(10), t(20)), (t(30), t(40)), (t(45), None)]
    # Same split rule as summarize(), so outages match the detail page.
    assert len(eps) == rds.summarize(segments, t(0), end)["incident_count"]


# ── A07: active critical is independent of range ───────────────────────────

def test_active_critical_now_ignores_window_and_honours_scope(monkeypatch):
    db = ReportDB([device(D1)], active_critical=4)
    data = run_executive(monkeypatch, db, FakeCH(), start=END - timedelta(hours=1))
    assert data["kpis"]["active_critical_count"] == data["kpis"]["active_critical_now"] == 4
    assert data["kpis"]["critical_raised_in_period"] == 0
    [(sql, params)] = [c for c in db.calls if "COUNT(*) FROM alerts" in c[0]]
    assert "triggered_at" not in sql
    asyncio.run(rds._active_critical_now(db, ["core"]))
    sql, params = db.calls[-1]
    assert scoping.alert_visible("alerts") in sql and params[scoping.SCOPE_PARAM] == ["core"]


# ── Scope, inventory, bounds ───────────────────────────────────────────────

def test_scoped_user_queries_only_visible_entities(monkeypatch):
    db = ReportDB([device(D1)])
    ch = FakeCH(samples={(START, END): {D1: (50.0, 100.0), D2: (100.0, 100.0)}})
    data = run_executive(monkeypatch, db, ch, visible_tags=["core"])
    dev_sql, dev_params = next(c for c in db.calls if "FROM devices d" in c[0])
    assert scoping.jsonb_tags_visible("d.tags") in dev_sql
    assert dev_params[scoping.SCOPE_PARAM] == ["core"]
    alert_sql, alert_params = next(c for c in db.calls if "FROM alerts a" in c[0])
    assert scoping.alert_visible("a") in alert_sql
    assert all(p.get("ids") == [D1] for s, p in ch.calls if "ids" in p)
    assert data["kpis"]["availability_pct"] == pytest.approx(50)


def test_service_checks_fetch_is_scoped():
    db = ReportDB([])
    asyncio.run(report_service._fetch_service_checks(db, visible_tags=["core"]))
    sql, params = db.calls[-1]
    assert scoping.text_tags_visible("sc.tags") in sql and params[scoping.SCOPE_PARAM] == ["core"]


def test_report_endpoint_passes_caller_scope(client, monkeypatch):
    seen = {}

    async def fake_build(db, f, t, visible_tags=None):
        seen["scope"] = visible_tags
        return {"ok": True}

    async def fake_perms(db, role):
        return []
    monkeypatch.setattr(reports_api, "build_business", fake_build)
    monkeypatch.setattr(scoping, "get_role_permissions", fake_perms)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=uuid4(), role="viewer", scope_tags=["Core"], username="v")
    try:
        assert client.get("/api/v1/reports/data/business").json() == {"ok": True}
        assert seen["scope"] == ["core"]
        assert client.get("/api/v1/reports/data/business",
                          params={"from": END.isoformat(), "to": START.isoformat()}).status_code == 400
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_generic_500_does_not_leak_exception(client, as_admin, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("password=hunter2")
    monkeypatch.setattr(reports_api, "build_executive", boom)
    r = client.get("/api/v1/reports/data/executive")
    assert r.status_code == 500 and "hunter2" not in r.text


def test_deleted_devices_do_not_move_the_headline(monkeypatch):
    ch = FakeCH(samples={(START, END): {D1: (100.0, 100.0), GONE: (0.0, 100.0)}}, honour_ids=False)
    data = run_executive(monkeypatch, ReportDB([device(D1)]), ch)
    assert data["kpis"]["availability_pct"] == 100
    assert data["kpis"]["devices_measured"] == 1
    assert sum(g["devices"] for g in data["group_summary"]) == 1


def test_group_and_type_summaries_use_explicit_bounds(monkeypatch):
    w1 = (START, END)
    w2 = (START - timedelta(days=7), END - timedelta(days=7))
    samples = {w1: {D1: (100.0, 100.0), D2: (90.0, 100.0)},
               w2: {D1: (50.0, 100.0), D2: (100.0, 100.0)}}
    devs = [device(D1, group="Core", dtype="router"), device(D2, group="Edge", dtype="switch", status="down")]
    one = run_executive(monkeypatch, ReportDB(devs), FakeCH(samples=samples), *w1)
    two = run_executive(monkeypatch, ReportDB(devs), FakeCH(samples=samples), *w2)
    g1 = {g["name"]: g for g in one["group_summary"]}
    g2 = {g["name"]: g for g in two["group_summary"]}
    assert g1["Core"]["availability_pct"] == 100 and g2["Core"]["availability_pct"] == 50
    assert g1["Edge"]["availability_pct"] == 90 and g2["Edge"]["availability_pct"] == 100
    assert {t["name"] for t in one["type_summary"]} == {"router", "switch"}
    assert g1["Edge"]["down_now"] == 1 and g1["Core"]["up_now"] == 1 and g1["Core"]["measured"] == 1
    assert one["kpis"]["availability_pct"] == pytest.approx(95)
    assert two["kpis"]["availability_pct"] == pytest.approx(75)
    assert one["status_now"] == {"up": 1, "down": 1, "degraded": 0, "maintenance": 0,
                                 "unknown": 0, "total": 2}


def test_configured_policy_target_drives_sla(monkeypatch):
    ch = FakeCH(samples={(START, END): {D1: (995.0, 1000.0)}})
    data = run_executive(monkeypatch, ReportDB([device(D1)], policy=99.0), ch)
    k = data["kpis"]
    assert k["sla_target_pct"] == k["configured_target_pct"] == 99.0
    assert k["target_source"] == "policy" and k["sla_met"] is True
    data = run_executive(monkeypatch, ReportDB([device(D1)]), ch)
    assert data["kpis"]["target_source"] == "default" and data["kpis"]["sla_met"] is False


# ── Business: compliance, impact, TLS ──────────────────────────────────────

@pytest.mark.parametrize("avail,target,down,covered,expected", [
    (None, 99.9, None, 0, "not_assessed"),
    (99.0, 99.9, 864, 86400, "breached"),
    (99.95, 99.9, 70, 86400, "at_risk"),      # 70s of an 86.4s budget = 81%
    (99.99, 99.9, 8.64, 86400, "met"),
])
def test_compliance_states(avail, target, down, covered, expected):
    meets, compliance, used = rds._compliance(avail, target, down, covered)
    assert compliance == expected
    assert meets is (None if avail is None else avail >= target)
    if expected == "at_risk":
        assert used > rds.AT_RISK_BUDGET_PCT


def service(sid, *, group=None, criticality=None, target=None, check_type="http",
            url="https://app.example", tls_days=None, warn=30, critical=7):
    return {"id": sid, "name": f"svc-{sid[:4]}", "check_type": check_type, "target_host": "h",
            "target_url": url, "status": "up", "created_at": None, "check_interval": 60,
            "group_id": uuid4() if group else None, "group_name": group, "criticality": criticality,
            "business_owner": None, "sla_target_pct": target, "tls_days_remaining": tls_days,
            "tls_expiry_date": None, "tls_warn_days": warn, "tls_critical_days": critical}


def run_business(monkeypatch, services, segments, policy=(99.9, "default")):
    async def fetch(db, visible_tags=None):
        return services

    async def load(db):
        return policy

    async def no_maint(db):
        return set()

    def fleet(ch, checks, start, end, basis="confirmed"):
        assert basis == "confirmed"
        for cid in checks:
            yield cid, segments.get(cid, []), 0, start

    from app.services import service_check_service
    monkeypatch.setattr(rds, "_fetch_service_checks", fetch)
    monkeypatch.setattr(rds, "load_default_target", load)
    monkeypatch.setattr(rds, "read_fleet_segments", fleet)
    monkeypatch.setattr(rds, "_fetch_service_metrics", lambda s, e, ids: [])
    monkeypatch.setattr(rds, "get_ch_client", lambda: None)
    monkeypatch.setattr(service_check_service, "active_maintenance_check_ids", no_maint)
    return asyncio.run(rds.build_business(None, START, END))


def _down(m0, m1):
    return [(START, START + timedelta(minutes=m0), 1.),
            (START + timedelta(minutes=m0), START + timedelta(minutes=m1), 0.),
            (START + timedelta(minutes=m1), END, 1.)]


def test_customer_impact_is_null_when_unmapped(monkeypatch):
    a, b = str(uuid4()), str(uuid4())
    data = run_business(monkeypatch, [service(a), service(b, group="Payments")],
                        {a: _down(60, 90), b: _down(70, 100)})
    assert data["customer_impact_minutes"] is None and data["impact_basis"] is None
    assert data["impact_assessed_services"] == 0 and data["impact_unassessed_services"] == 2
    # Elapsed time with at least one service confirmed down, overlaps merged.
    assert data["confirmed_downtime_minutes"] == pytest.approx(40)
    rows = {r["service_check_id"]: r for r in data["service_availability"]}
    assert rows[a]["outages"] == 1 and rows[a]["downtime_seconds"] == pytest.approx(1800)
    assert rows[a]["availability_basis"] == "confirmed" and rows[a]["probe_availability_pct"] is None
    [first] = [o for o in data["service_outages"] if o["service_check_id"] == a]
    assert first["duration_minutes"] == pytest.approx(30) and first["ongoing"] is False


def test_customer_impact_merges_overlaps_per_group(monkeypatch):
    a, b, c = str(uuid4()), str(uuid4()), str(uuid4())
    data = run_business(monkeypatch, [
        service(a, group="Payments", criticality="critical"),
        service(b, group="Payments", criticality="high"),
        service(c, group="Search", criticality="low"),
    ], {a: _down(60, 90), b: _down(70, 100), c: _down(60, 70)})
    assert data["impact_basis"] == "confirmed_downtime_merged"
    assert data["impact_assessed_services"] == 3
    assert data["customer_impact_minutes"] == pytest.approx(40 + 10)


def test_service_targets_and_compliance_summary(monkeypatch):
    met, breached, unmeasured = str(uuid4()), str(uuid4()), str(uuid4())
    data = run_business(monkeypatch, [
        service(met, target=95.0), service(breached), service(unmeasured),
    ], {met: _down(60, 90), breached: _down(60, 90)}, policy=(99.5, "policy"))
    rows = {r["service_check_id"]: r for r in data["service_availability"]}
    assert rows[met]["target_pct"] == 95.0 and rows[met]["target_source"] == "service"
    assert rows[met]["compliance"] == "met" and rows[met]["meets_target"] is True
    assert rows[breached]["target_source"] == "policy" and rows[breached]["compliance"] == "breached"
    assert rows[unmeasured]["availability_pct"] is None
    assert rows[unmeasured]["compliance"] == "not_assessed" and rows[unmeasured]["meets_target"] is None
    assert data["compliance_summary"] == {"met": 1, "at_risk": 0, "breached": 1,
                                          "not_assessed": 1, "eligible": 2}


def test_tls_evidence_and_per_check_thresholds(monkeypatch):
    s = [service(str(uuid4()), tls_days=40, warn=45, critical=10),
         service(str(uuid4()), check_type="tls", url=None, tls_days=5),
         service(str(uuid4()), tls_days=None),
         service(str(uuid4()), url="http://plain.example"),
         service(str(uuid4()), check_type="tcp", url=None)]
    data = run_business(monkeypatch, s, {})
    assert data["tls_evidence"] == {"eligible": 3, "with_expiry_data": 2}
    assert [(w["days_remaining"], w["severity"]) for w in data["tls_warnings"]] == [
        (5, "critical"), (40, "warning")]


# ── Policy endpoint ─────────────────────────────────────────────────────────

def test_policy_defaults_without_configuration(client, as_viewer):
    assert client.get("/api/v1/reports/availability-policy").json() == {
        "default_target_pct": 99.9, "source": "default"}


@pytest.mark.parametrize("value", [49.9, 100.01, "abc"])
def test_policy_rejects_out_of_range(client, as_operator, value):
    r = client.put("/api/v1/reports/availability-policy", json={"default_target_pct": value})
    assert r.status_code == 422


def test_policy_requires_operator(client, as_viewer):
    r = client.put("/api/v1/reports/availability-policy", json={"default_target_pct": 99.5})
    assert r.status_code == 403


def test_policy_update_is_saved_and_audited(client, as_operator, monkeypatch):
    saved, audited = [], []

    async def save(db, value):
        saved.append(value)

    async def audit(db, **kw):
        audited.append(kw)
    monkeypatch.setattr(availability_policy, "save_default_target", save)
    monkeypatch.setattr(reports_api, "write_audit_log", audit)
    r = client.put("/api/v1/reports/availability-policy", json={"default_target_pct": 99.5})
    assert r.status_code == 200 and r.json()["default_target_pct"] == 99.5
    assert saved == [99.5]
    assert audited[0]["action"] == "reports.availability_policy.update"
    assert audited[0]["resource_id"] == availability_policy.SETTINGS_KEY


def test_policy_value_coercion():
    assert availability_policy._coerce(99.5) == 99.5
    assert availability_policy._coerce({"default_target_pct": "99.95"}) == 99.95
    assert availability_policy._coerce(10) is None
    assert availability_policy.resolve_target(None, 99.5, "policy") == (99.5, "policy")
    assert availability_policy.resolve_target(98, 99.5, "policy") == (98.0, "service")


# ── Service objectives on the service-check API ─────────────────────────────

def test_service_check_schemas_accept_objectives():
    from pydantic import ValidationError
    from app.schemas.service_check import ServiceCheckCreate, ServiceCheckUpdate

    base = {"name": "web", "check_type": "http", "target_host": "h", "target_url": "https://h"}
    sc = ServiceCheckCreate(**base, sla_target_pct=99.95, criticality="critical", business_owner="  ")
    assert (sc.sla_target_pct, sc.criticality, sc.business_owner) == (99.95, "critical", None)
    for bad in ({"sla_target_pct": 0}, {"sla_target_pct": 100.5}, {"criticality": "urgent"},
                {"business_owner": "x" * 256}):
        with pytest.raises(ValidationError):
            ServiceCheckCreate(**base, **bad)
    update = ServiceCheckUpdate(sla_target_pct=None, criticality="low")
    assert update.model_dump(exclude_unset=True) == {"sla_target_pct": None, "criticality": "low"}


def test_objective_only_update_keeps_confirmation_progress():
    from app.models.service_check import ServiceCheck
    from app.schemas.service_check import ServiceCheckUpdate
    from app.services import service_check_service

    sc = ServiceCheck(id=uuid4(), name="web", check_type="http", target_host="h", enabled=True,
                      http_follow_redirects=True, verification_attempt=2,
                      created_at=START, tags=[], workflow_steps=[])

    class DB:
        async def execute(self, *a, **k):
            return SimpleNamespace(scalar_one_or_none=lambda: sc)

        async def commit(self):
            pass

        async def refresh(self, obj):
            pass

    out = asyncio.run(service_check_service.update_service_check(
        DB(), sc.id, ServiceCheckUpdate(sla_target_pct=99.5, criticality="high", business_owner="Ops")))
    assert (out.sla_target_pct, out.criticality, out.business_owner) == (99.5, "high", "Ops")
    assert sc.verification_attempt == 2
    asyncio.run(service_check_service.update_service_check(DB(), sc.id, ServiceCheckUpdate(timeout=5)))
    assert sc.verification_attempt == 0


# ── Device uptime-stats honours explicit bounds ─────────────────────────────

def test_device_uptime_stats_explicit_bounds(client, as_admin, monkeypatch):
    from app.core import database
    calls = []

    def query(sql, parameters=None):
        calls.append(parameters)
        return SimpleNamespace(result_rows=[(D1, 99.0, 100.0)])
    monkeypatch.setattr(database, "get_clickhouse_client", lambda: SimpleNamespace(query=query))
    frm, to = START - timedelta(days=10), START - timedelta(days=9)
    r = client.get("/api/v1/devices/dashboard/uptime-stats",
                   params={"from": frm.isoformat(), "to": to.isoformat()})
    assert r.status_code == 200
    assert calls[0] == {"from": naive(frm), "to": naive(to)}
    assert r.json()["devices"] == {D1: 99.0}
    bad = client.get("/api/v1/devices/dashboard/uptime-stats",
                     params={"from": to.isoformat(), "to": frm.isoformat()})
    assert bad.status_code == 400


def test_mapped_but_unmeasured_impact_is_null(monkeypatch):
    a = str(uuid4())
    data = run_business(monkeypatch, [service(a, group="Payments", criticality="critical")], {})
    assert data["impact_assessed_services"] == 1
    assert data["customer_impact_minutes"] is None
    assert data["confirmed_downtime_minutes"] is None


def test_html_report_renders_unmeasured_executive(monkeypatch):
    from app.services import report_html
    data = run_executive(monkeypatch, ReportDB([device(D1)]), FakeCH())
    html = report_html.build_report_html(
        {"title": "Executive", "report_type": "executive_summary", "period": "last_24h",
         "period_label": "Last 24h", "generated_at": END, "company_name": "Acme",
         "product_name": "ZenPlus"}, data)
    assert "0.0%" not in html and "0%" not in html.split("Availability", 1)[1][:400]


def test_longest_outages_section_shows_unknown_and_ongoing():
    from app.services import report_sections

    class Ctx:
        async def dataset(self, name):
            return {"outage_timeline": [
                {"hostname": "a", "started_at": START.isoformat(), "duration_minutes": None,
                 "in_window_minutes": 5, "ongoing": False},
                {"hostname": "b", "started_at": START.isoformat(), "duration_minutes": 90,
                 "ongoing": True},
            ]}
    section = asyncio.run(report_sections._sec_top_outages(Ctx()))
    rows = section["tables"][0]["rows"]
    assert rows[0][2] == "90 min (ongoing)" and rows[1][2] == "unknown"


@pytest.mark.parametrize("visible", [[D1], []])
def test_device_uptime_stats_is_scoped(client, monkeypatch, visible):
    from app.core import database
    calls, db_calls = [], []

    def query(sql, parameters=None):
        calls.append((sql, parameters))
        ids = parameters.get("ids")
        rows = [(D1, 99.0, 100.0), (D2, 50.0, 100.0)]
        return SimpleNamespace(result_rows=[r for r in rows if ids is None or r[0] in ids])

    class ScopeDB:
        async def execute(self, statement, params=None):
            db_calls.append((str(statement), params))
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: visible),
                                   all=lambda: [])

    async def scope_db():
        yield ScopeDB()

    async def no_perms(db, role):
        return []
    monkeypatch.setattr(database, "get_clickhouse_client", lambda: SimpleNamespace(query=query))
    monkeypatch.setattr(scoping, "get_role_permissions", no_perms)
    app.dependency_overrides[get_db] = scope_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=uuid4(), role="viewer", scope_tags=["Core"], username="v")
    try:
        r = client.get("/api/v1/devices/dashboard/uptime-stats", params={"hours": 24})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 200
    sql, params = db_calls[0]
    assert scoping.jsonb_tags_visible("d.tags") in sql and params[scoping.SCOPE_PARAM] == ["core"]
    if visible:
        assert r.json()["devices"] == {D1: 99.0}
        assert all(p["ids"] == [D1] and "IN %(ids)s" in q for q, p in calls)
    else:
        assert r.json()["devices"] == {} and calls == []
