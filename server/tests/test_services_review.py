"""Services development review (B01-B27): scope, validation, maintenance,
SLA populations, metrics aggregation, incidents, bulk ops, audit, export/import.

No database: small fakes answer the ORM/ClickHouse reads each path makes.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.core import scoping
from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app
from app.models.service_check import ServiceCheck, ServiceCheckMaintenance
from app.services import audit_service, service_check_service, service_maintenance
from app.services.service_availability import (
    confirmed_segments, daily_summaries, incident_intervals, summarize,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


# ── fakes ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_real_clickhouse(monkeypatch):
    """Fail loudly instead of ever reaching an installed ClickHouse."""
    from app.core import database
    from app.services import service_metric_service

    def refuse(*a, **k):
        raise AssertionError("test attempted a real ClickHouse connection")
    monkeypatch.setattr(database, "get_clickhouse_client", refuse)
    monkeypatch.setattr(database, "get_ch_client", refuse)
    monkeypatch.setattr(service_metric_service, "get_clickhouse_client", refuse)


class Res:
    def __init__(self, rows=(), rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self.rows), first=lambda: self.rows[0] if self.rows else None)

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def mappings(self):
        return SimpleNamespace(all=lambda: list(self.rows), first=lambda: self.rows[0] if self.rows else None)


def _param_ids(stmt) -> set:
    try:
        params = stmt.compile().params
    except Exception:
        return set()
    out = set()
    for v in params.values():
        if isinstance(v, UUID):
            out.add(v)
        elif isinstance(v, (list, tuple)):
            out.update(x for x in v if isinstance(x, UUID))
    return out


class CheckDB:
    """Answers entity selects on service_checks by id; records mutations."""

    def __init__(self, checks=(), audit_rows=()):
        self.checks = {c.id: c for c in checks}
        self.audit_rows = list(audit_rows)
        self.statements, self.mutations = [], []
        self.commits = 0
        self.added = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.statements.append(sql)
        head = sql.lstrip().upper()
        if head.startswith(("DELETE", "UPDATE", "INSERT")):
            self.mutations.append(sql)
            return Res(rowcount=len(_param_ids(stmt)))
        if "COUNT(*) FROM audit_logs" in sql:
            return Res([len(self.audit_rows)])
        if "FROM audit_logs" in sql:
            return Res(self.audit_rows)
        if sql.lstrip().startswith("SELECT service_checks.id, service_checks.device_id"):
            ids = _param_ids(stmt)
            return Res([c for i, c in self.checks.items() if not ids or i in ids])
        return Res()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def flush(self):
        pass

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        if hasattr(obj, "created_at") and obj.created_at is None:
            obj.created_at = NOW

    def add(self, obj):
        self.added.append(obj)

    def begin_nested(self):
        class Ctx:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False
        return Ctx()


def check(name="svc", tags=("team-a",), **kw):
    base = dict(id=uuid4(), name=name, check_type="http", target_host="h", target_url="https://h/",
                enabled=True, tags=list(tags), http_method="GET", http_expected_status=200,
                http_follow_redirects=True, tls_warn_days=30, tls_critical_days=7, check_interval=60,
                timeout=10, status="up", created_at=NOW - timedelta(days=60), workflow_steps=[],
                verification_attempt=0, level=2, retry_count=3, retry_delay_s=10, down_check_interval=30,
                config={}, http_headers={}, http_body=None)
    base.update(kw)
    return ServiceCheck(**base)


@pytest.fixture
def api(monkeypatch):
    """TestClient without lifespan; scoped to team-a unless a test overrides."""
    state = {"db": CheckDB(), "scope": ["team-a"], "role": "operator"}

    async def fake_db():
        yield state["db"]

    async def user():
        return SimpleNamespace(id=uuid4(), username="op", full_name="Op User", role=state["role"],
                               is_active=True, scope_tags=state["scope"])

    async def visible(db, u):
        return state["scope"]

    monkeypatch.setattr(scoping, "visible_tags", visible)
    audits = []

    async def audit(db, **kw):
        audits.append(kw)

    monkeypatch.setattr(audit_service, "write_audit_log", audit)
    state["audits"] = audits
    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = user
    state["client"] = TestClient(app)
    yield state
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


# ── B02: scope on every route ───────────────────────────────────────────────

def _routes(cid):
    base = f"/api/v1/service-checks/{cid}"
    return [
        ("get", base, None),
        ("put", base, {"name": "x"}),
        ("delete", base, None),
        ("post", f"{base}/test", None),
        ("get", f"{base}/related", None),
        ("get", f"{base}/metrics", None),
        ("get", f"{base}/status-history", None),
        ("get", f"{base}/hourly-uptime", None),
        ("get", f"{base}/sla", None),
        ("get", f"{base}/incidents", None),
        ("get", f"{base}/latest-probes", None),
        ("get", f"{base}/maintenance", None),
        ("get", f"{base}/notes", None),
        ("post", f"{base}/notes", {"body": "looking"}),
        ("get", f"{base}/history", None),
        ("get", f"{base}/report", None),
    ]


def test_scoped_user_gets_404_for_out_of_scope_check_on_every_route(api):
    other = check("team-b svc", tags=["team-b"])
    api["db"] = CheckDB([other])
    for method, path, body in _routes(other.id):
        kw = {"json": body} if body is not None else {}
        r = getattr(api["client"], method)(path, **kw)
        assert r.status_code == 404, (method, path, r.status_code, r.text)
    assert api["db"].mutations == [] and api["db"].commits == 0
    assert api["audits"] == []


def test_bulk_delete_with_one_out_of_scope_id_is_404_and_deletes_nothing(api):
    mine, other = check("mine"), check("theirs", tags=["team-b"])
    api["db"] = CheckDB([mine, other])
    r = api["client"].post("/api/v1/service-checks/bulk-delete", json={"check_ids": [str(mine.id), str(other.id)]})
    assert r.status_code == 404
    assert api["db"].mutations == [] and api["db"].commits == 0


@pytest.mark.parametrize("action,extra", [("pause", {}), ("delete", {}), ("add_tags", {"tags": ["x"]}),
                                          ("maintenance", {"maintenance": {"duration_minutes": 30}})])
def test_bulk_mixed_scope_is_404_and_modifies_nothing(api, action, extra):
    mine, other = check("mine"), check("theirs", tags=["team-b"])
    api["db"] = CheckDB([mine, other])
    r = api["client"].post("/api/v1/service-checks/bulk",
                           json={"ids": [str(mine.id), str(other.id)], "action": action, **extra})
    assert r.status_code == 404
    assert mine.enabled is True and mine.tags == ["team-a"]
    assert api["db"].mutations == [] and api["db"].commits == 0 and api["db"].added == []


def test_bulk_pause_returns_per_item_results_and_audits(api):
    a, b = check("a"), check("b")
    api["db"] = CheckDB([a, b])
    r = api["client"].post("/api/v1/service-checks/bulk", json={"ids": [str(a.id), str(b.id)], "action": "pause"})
    assert r.status_code == 200
    body = r.json()
    assert body["succeeded"] == 2 and body["failed"] == 0
    assert {x["name"] for x in body["results"]} == {"a", "b"}
    assert a.enabled is False and b.enabled is False
    assert [x["action"] for x in api["audits"]] == ["service_check.bulk.pause"] * 2
    assert api["db"].commits == 1


def test_bulk_test_is_capped(api):
    checks = [check(f"c{i}") for i in range(26)]
    api["db"] = CheckDB(checks)
    r = api["client"].post("/api/v1/service-checks/bulk",
                           json={"ids": [str(c.id) for c in checks], "action": "test"})
    assert r.status_code == 422


def test_export_filters_out_of_scope_rows(api):
    api["db"] = CheckDB([check("mine"), check("theirs", tags=["team-b"])])
    rows = api["client"].get("/api/v1/service-checks/export/json").json()
    assert [r["name"] for r in rows] == ["mine"]
    doc = api["client"].get("/api/v1/service-checks/export/json?format=portable").json()
    assert doc["schema_version"] == 2 and [c["name"] for c in doc["checks"]] == ["mine"]


def test_history_lists_audit_entries_with_changes(api):
    mine = check("mine")
    api["db"] = CheckDB([mine], audit_rows=[{
        "id": uuid4(), "created_at": NOW, "actor_username": "op", "actor_role": "operator",
        "action": "service_check.update", "resource_type": "service_check",
        "metadata": {"name": "mine", "changes": {"timeout": {"old": 10, "new": 5}}},
    }])
    body = api["client"].get(f"/api/v1/service-checks/{mine.id}/history").json()
    assert body["meta"]["total"] == 1
    assert body["data"][0]["changes"] == {"timeout": {"old": 10, "new": 5}}
    assert body["data"][0]["metadata"] == {"name": "mine"}


def test_notes_are_created_and_audited(api):
    mine = check("mine")
    api["db"] = CheckDB([mine])
    r = api["client"].post(f"/api/v1/service-checks/{mine.id}/notes",
                           json={"body": "Rolled back deploy", "assignee": "  "})
    assert r.status_code == 201
    assert r.json()["body"] == "Rolled back deploy" and r.json()["assignee"] is None
    assert api["audits"][0]["action"] == "service_check.note"
    assert api["client"].post(f"/api/v1/service-checks/{mine.id}/notes", json={"body": "   "}).status_code == 422


# ── B01: clock ──────────────────────────────────────────────────────────────

def test_system_time_and_server_time_header(api, monkeypatch):
    from app.api.v1 import system_time
    monkeypatch.setattr(system_time, "read_timedatectl", lambda timeout=2.0: {
        "source": "timedatectl", "timezone": "Asia/Riyadh", "ntp_synchronized": False, "ntp_service": "active"})
    r = api["client"].get("/api/v1/system/time")
    assert r.status_code == 200
    body = r.json()
    assert body["ntp_synchronized"] is False and body["source"] == "timedatectl"
    assert body["timezone"] == "Asia/Riyadh" and body["server_time"]
    assert int(r.headers["x-server-time"]) > 0


def test_timedatectl_parse_never_raises(monkeypatch):
    import subprocess
    from app.api.v1 import system_time

    def boom(*a, **k):
        raise FileNotFoundError("timedatectl")
    monkeypatch.setattr(subprocess, "run", boom)
    assert system_time.read_timedatectl() == {"source": "unavailable"}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="NTP=no\nNTPSynchronized=no\nTimezone=UTC\n"))
    assert system_time.read_timedatectl() == {"source": "timedatectl", "timezone": "UTC",
                                              "ntp_synchronized": False, "ntp_service": "inactive"}


# ── B05: type, level, cycles, scope tags ────────────────────────────────────

class SingleDB(CheckDB):
    """update_service_check reads the row with a plain select."""

    def __init__(self, sc, parents=None):
        super().__init__([sc])
        self.sc, self.parents = sc, parents or {}

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if sql.lstrip().startswith("SELECT service_checks.id, service_checks.name, service_checks.parent_check_id"):
            ids = _param_ids(stmt)
            for pid in ids:
                if pid in self.parents:
                    return Res([self.parents[pid]])
            return Res()
        return await super().execute(stmt, params)


def test_type_change_is_rejected_not_ignored():
    from app.schemas.service_check import ServiceCheckUpdate
    sc = check("web")
    with pytest.raises(service_check_service.ServiceCheckValidationError) as exc:
        asyncio.run(service_check_service.update_service_check(SingleDB(sc), sc.id, ServiceCheckUpdate(check_type="tcp")))
    assert str(exc.value) == "Monitor type cannot be changed. Duplicate the monitor to create a TCP check."
    # Same type is fine (old UIs send it back unchanged).
    out = asyncio.run(service_check_service.update_service_check(
        SingleDB(sc), sc.id, ServiceCheckUpdate(check_type="http", timeout=5)))
    assert out.timeout == 5 and out.check_type == "http"


def test_type_change_route_is_422(api):
    sc = check("web")
    api["db"] = SingleDB(sc)
    r = api["client"].put(f"/api/v1/service-checks/{sc.id}", json={"check_type": "tcp", "name": "x"})
    assert r.status_code == 422 and "Duplicate the monitor" in r.json()["detail"]


def test_level_is_derived_by_the_server():
    from app.schemas.service_check import ServiceCheckUpdate, ServiceWorkflowStep
    d = service_check_service.derive_level
    assert (d("icmp", []), d("tcp", []), d("http", []), d("tls", []), d("dns", [])) == (1, 1, 2, 2, 2)
    step = ServiceWorkflowStep(name="home", url="https://h/")
    assert d("http", [step]) == 3
    sc = check("web", level=1)
    out = asyncio.run(service_check_service.update_service_check(
        SingleDB(sc), sc.id, ServiceCheckUpdate(level=1, description="x")))
    assert out.level == 2  # client value ignored
    assert sc.verification_attempt == 0


def test_create_derives_level_and_enforces_scope_tags(monkeypatch):
    from app.schemas.service_check import ServiceCheckCreate
    from app.services import tag_service

    async def canon(db, tags):
        return list(tags or [])
    monkeypatch.setattr(tag_service, "canonicalize_tags", canon)
    db = CheckDB()
    data = ServiceCheckCreate(name="ping", check_type="icmp", target_host="h", level=3, tags=["team-a"])
    out = asyncio.run(service_check_service.create_service_check(db, data, uuid4(), visible_tags=["team-a"]))
    assert out.level == 1
    hidden = ServiceCheckCreate(name="ping", check_type="icmp", target_host="h", tags=["team-b"])
    with pytest.raises(service_check_service.ServiceCheckValidationError, match="scope tags"):
        asyncio.run(service_check_service.create_service_check(db, hidden, uuid4(), visible_tags=["team-a"]))


def test_dependency_cycles_are_rejected():
    from app.schemas.service_check import ServiceCheckUpdate
    a, b = check("A"), check("B")
    b_row = (b.id, "B", a.id, ["team-a"])
    with pytest.raises(service_check_service.ServiceCheckValidationError, match="Dependency cycle: A → B → A"):
        asyncio.run(service_check_service.update_service_check(
            SingleDB(a, {b.id: b_row}), a.id, ServiceCheckUpdate(parent_check_id=b.id)))
    with pytest.raises(service_check_service.ServiceCheckValidationError, match="Dependency cycle: A → A"):
        asyncio.run(service_check_service.update_service_check(
            SingleDB(a), a.id, ServiceCheckUpdate(parent_check_id=a.id)))
    c = check("C")
    c_row = (c.id, "C", None, ["team-a"])
    out = asyncio.run(service_check_service.update_service_check(
        SingleDB(a, {c.id: c_row}), a.id, ServiceCheckUpdate(parent_check_id=c.id)))
    assert out.parent_check_id == c.id


def test_runbook_must_be_http_url():
    from pydantic import ValidationError
    from app.schemas.service_check import ServiceCheckUpdate
    assert ServiceCheckUpdate(runbook_url="https://wiki/x").runbook_url == "https://wiki/x"
    assert ServiceCheckUpdate(runbook_url="  ").runbook_url is None
    with pytest.raises(ValidationError):
        ServiceCheckUpdate(runbook_url="javascript:alert(1)")


def test_update_changes_are_diffed_with_secrets_redacted():
    from app.schemas.service_check import ServiceCheckUpdate
    sc = check("web", http_headers={"Authorization": "Bearer old", "Accept": "a"})
    changes = {}
    asyncio.run(service_check_service.update_service_check(
        SingleDB(sc), sc.id,
        ServiceCheckUpdate(timeout=5, http_headers={"Authorization": "Bearer new", "Accept": "b"},
                           http_body="password=hunter2"),
        changes_out=changes))
    assert changes["timeout"] == {"old": 10, "new": 5}
    assert changes["http_headers"]["new"] == {"Authorization": "__REDACTED__", "Accept": "b"}
    assert "hunter2" not in str(changes)


# ── B03/B04/B24: maintenance ────────────────────────────────────────────────

S = NOW - timedelta(hours=10)


def _m(minutes):
    return S + timedelta(minutes=minutes)


def test_downtime_fully_inside_maintenance_does_not_count():
    segs = [(_m(0), _m(60), 1.), (_m(60), _m(90), 0.), (_m(90), _m(180), 1.)]
    stats = summarize(segs, _m(0), _m(180), [(_m(55), _m(95))])
    assert stats["total_downtime_sec"] == 0 and stats["uptime_pct"] == 100
    assert stats["maintenance_sec"] == 40 * 60
    assert stats["covered_sec"] == 140 * 60 and stats["unknown_sec"] == 0


def test_partial_overlap_counts_only_the_outside_part_and_windows_merge():
    segs = [(_m(0), _m(60), 1.), (_m(60), _m(90), 0.), (_m(90), _m(180), 1.)]
    # Two overlapping windows covering 50..75 are counted once (25 min).
    stats = summarize(segs, _m(0), _m(180), [(_m(50), _m(70)), (_m(65), _m(75))])
    assert stats["maintenance_sec"] == 25 * 60
    assert stats["total_downtime_sec"] == 15 * 60
    assert stats["incident_count"] == 1


def test_gaps_stay_unknown_with_maintenance():
    segs = [(_m(0), _m(30), 1.), (_m(90), _m(120), 1.)]
    stats = summarize(segs, _m(0), _m(120), [(_m(100), _m(110))])
    assert stats["unknown_sec"] == 60 * 60
    assert stats["maintenance_sec"] == 10 * 60
    assert stats["covered_sec"] == 50 * 60


def test_outage_split_only_by_maintenance_is_one_incident():
    segs = [(_m(0), _m(10), 0.), (_m(10), _m(20), 0.), (_m(20), _m(30), 0.)]
    stats = summarize(segs, _m(0), _m(30), [(_m(10), _m(20))])
    assert stats["incident_count"] == 1 and stats["total_downtime_sec"] == 20 * 60


def test_confirmed_and_daily_rows_report_maintenance():
    from zoneinfo import ZoneInfo
    segs = confirmed_segments([(_m(0), _m(120), 1.)], [(_m(0), "up"), (_m(30), "down"), (_m(60), "up")])
    rows = daily_summaries(segs, _m(0), _m(120), ZoneInfo("UTC"), [(_m(30), _m(60))])
    assert sum(r["maintenance_sec"] for r in rows) == 30 * 60
    assert all(r["downtime_sec"] == 0 for r in rows)


def test_business_episodes_exclude_maintenance():
    from app.services.report_data_service import confirmed_down_episodes
    segs = [(_m(0), _m(10), 0.), (_m(10), _m(20), 0.), (_m(20), _m(30), 1.)]
    assert confirmed_down_episodes(segs, _m(30), [(_m(5), _m(10))]) == [(_m(0), _m(20))]
    assert confirmed_down_episodes(segs, _m(30), [(_m(0), _m(20))]) == []


class MaintDB(CheckDB):
    def __init__(self, window=None, checks=()):
        super().__init__(checks)
        self.window = window

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM service_check_maintenance" in sql:
            return Res([self.window] if self.window is not None else [])
        return await super().execute(stmt, params)


def _window(starts, ends, **kw):
    return ServiceCheckMaintenance(id=uuid4(), scope_type=kw.pop("scope_type", "check"),
                                   scope_check_id=kw.pop("scope_check_id", uuid4()),
                                   starts_at=starts, ends_at=ends, created_at=starts, **kw)


def test_end_active_window_keeps_record_and_effective_end():
    m = _window(NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    db = MaintDB(m)
    out, prior = asyncio.run(service_maintenance.end_window(db, m.id, uuid4(), "done early", None, now=NOW))
    assert prior == "active" and out.ended_at == NOW and out.end_reason == "done early"
    assert service_maintenance.effective_end(out) == NOW
    assert service_maintenance.window_state(out, NOW + timedelta(minutes=1)) == "ended"
    assert db.mutations == []  # nothing deleted
    with pytest.raises(service_maintenance.MaintenanceError) as exc:
        asyncio.run(service_maintenance.end_window(db, m.id, uuid4(), None, None, now=NOW + timedelta(minutes=1)))
    assert exc.value.status_code == 409
    # The excluded interval stays reproducible: [starts, ended_at).
    spans = service_maintenance.intervals_from_windows([out], {"id": m.scope_check_id}, S, NOW + timedelta(hours=2))
    assert spans == [(NOW - timedelta(hours=1), NOW)]


def test_end_scheduled_window_cancels_it():
    m = _window(NOW + timedelta(hours=1), NOW + timedelta(hours=2))
    out, prior = asyncio.run(service_maintenance.end_window(MaintDB(m), m.id, uuid4(), None, None, now=NOW))
    assert prior == "scheduled" and out.cancelled_at == NOW
    assert service_maintenance.window_state(out, NOW) == "cancelled"
    assert service_maintenance.intervals_from_windows([out], {"id": m.scope_check_id}, S, NOW + timedelta(hours=3)) == []


def test_delete_of_started_window_is_409_and_unstarted_is_cancelled():
    started = _window(NOW - timedelta(minutes=5), NOW + timedelta(hours=1))
    with pytest.raises(service_maintenance.MaintenanceError) as exc:
        asyncio.run(service_maintenance.cancel_window(MaintDB(started), started.id, uuid4(), None, now=NOW))
    assert exc.value.status_code == 409 and "history is kept" in exc.value.detail
    future = _window(NOW + timedelta(hours=1), NOW + timedelta(hours=2))
    out = asyncio.run(service_maintenance.cancel_window(MaintDB(future), future.id, uuid4(), None, now=NOW))
    assert out.cancelled_at == NOW


def test_patch_rejects_past_end():
    from app.schemas.service_check import ServiceMaintenancePatch
    m = _window(NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    with pytest.raises(service_maintenance.MaintenanceError) as exc:
        asyncio.run(service_maintenance.patch_window(MaintDB(m), m.id,
                                                     ServiceMaintenancePatch(ends_at=NOW - timedelta(minutes=1)), now=NOW))
    assert exc.value.status_code == 422
    out, changes = asyncio.run(service_maintenance.patch_window(
        MaintDB(m), m.id, ServiceMaintenancePatch(ends_at=NOW + timedelta(hours=3)), now=NOW))
    assert out.ends_at == NOW + timedelta(hours=3) and "ends_at" in changes


def test_create_window_validation():
    from app.schemas.service_check import ServiceMaintenanceCreate
    sc = check("mine")
    db = MaintDB(checks=[sc])
    bad = ServiceMaintenanceCreate(scope_type="check", scope_check_id=sc.id,
                                   starts_at=NOW + timedelta(hours=2), ends_at=NOW + timedelta(hours=1))
    with pytest.raises(service_maintenance.MaintenanceError, match="after starts_at"):
        asyncio.run(service_maintenance.create_window(db, bad, None, None, now=NOW))
    with pytest.raises(service_maintenance.MaintenanceError, match="scope_check_id"):
        asyncio.run(service_maintenance.create_window(
            db, ServiceMaintenanceCreate(scope_type="check", duration_minutes=10), None, None, now=NOW))
    with pytest.raises(service_maintenance.MaintenanceError, match="every monitor"):
        asyncio.run(service_maintenance.create_window(
            db, ServiceMaintenanceCreate(scope_type="all", duration_minutes=10), None, ["team-a"], now=NOW))
    m = asyncio.run(service_maintenance.create_window(
        db, ServiceMaintenanceCreate(scope_type="check", scope_check_id=sc.id, duration_minutes=30), None,
        ["team-a"], now=NOW))
    assert m.starts_at == NOW and m.ends_at == NOW + timedelta(minutes=30)
    hidden = check("theirs", tags=["team-b"])
    with pytest.raises(service_maintenance.MaintenanceError, match="Monitor not found"):
        asyncio.run(service_maintenance.create_window(
            MaintDB(checks=[hidden]),
            ServiceMaintenanceCreate(scope_type="check", scope_check_id=hidden.id, duration_minutes=5),
            None, ["team-a"], now=NOW))


def test_window_coverage_by_scope():
    sc = {"id": uuid4(), "group_id": uuid4(), "tags": ["db"]}
    covers = service_maintenance.covers
    assert covers(_window(S, NOW, scope_type="all", scope_check_id=None), sc)
    assert covers(_window(S, NOW, scope_type="group", scope_check_id=None, scope_group_id=sc["group_id"]), sc)
    assert covers(_window(S, NOW, scope_type="tag", scope_check_id=None, scope_tag="db"), sc)
    assert not covers(_window(S, NOW, scope_type="tag", scope_check_id=None, scope_tag="web"), sc)
    assert covers(_window(S, NOW, scope_check_id=sc["id"]), sc)


# ── SLA: bounds, latency nulls, populations, SLO ────────────────────────────

class SlaCH:
    def __init__(self, latency=(0, math.nan, math.nan, 0.0, 0.0)):
        self.latency = latency
        self.calls = []

    def query(self, sql, parameters=None):
        self.calls.append(sql)
        if "quantile(0.95)(response_ms)" in sql:
            return SimpleNamespace(result_rows=[self.latency])
        return SimpleNamespace(result_rows=[])


def _sla(monkeypatch, segments, ch=None, excluded=None, target=None, **kw):
    from app.core import database
    from app.services import availability_policy, service_availability
    sc = check("web", sla_target_pct=target, created_at=NOW - timedelta(days=90))
    monkeypatch.setattr(database, "get_clickhouse_client", lambda: ch or SlaCH())
    monkeypatch.setattr(service_check_service, "_utcnow", lambda: NOW)
    monkeypatch.setattr(service_availability, "read_segments", lambda *a, **k: (segments, len(segments)))

    async def maint(db, checks, start, end):
        return {str(sc.id): excluded} if excluded else {}

    async def policy(db):
        return 99.0, "policy"
    monkeypatch.setattr(service_maintenance, "intervals_for_checks", maint)
    monkeypatch.setattr(availability_policy, "load_default_target", policy)
    return asyncio.run(service_check_service.get_service_sla(CheckDB([sc]), sc.id, 10, **kw))


def test_sla_rejects_reversed_or_half_bounds(monkeypatch):
    with pytest.raises(service_check_service.ServiceCheckValidationError):
        _sla(monkeypatch, [], from_dt=NOW, to_dt=NOW - timedelta(hours=1))
    with pytest.raises(service_check_service.ServiceCheckValidationError):
        _sla(monkeypatch, [], from_dt=NOW - timedelta(hours=1))


def test_sla_route_reversed_bounds_is_422(api):
    sc = check("mine")
    api["db"] = CheckDB([sc])
    r = api["client"].get(f"/api/v1/service-checks/{sc.id}/sla",
                          params={"from": "2026-09-30T10:00:00Z", "to": "2026-09-30T09:00:00Z"})
    assert r.status_code == 422


def test_empty_latency_is_null_not_zero(monkeypatch):
    out = _sla(monkeypatch, [])
    assert out["latency_sample_count"] == 0
    assert out["avg_response_ms"] is None and out["p95_response_ms"] is None
    assert out["max_response_ms"] is None and out["min_response_ms"] is None
    assert out["uptime_pct"] is None and out["meets_target"] is None
    assert out["requested_sec"] == 36000 and out["effective_to"] == NOW.isoformat()


def _h(hours, minutes=0):
    return S + timedelta(hours=hours, minutes=minutes)


def test_sla_incident_populations(monkeypatch):
    segments = [
        (_h(0), _h(0, 10), 0.), (_h(0, 10), _h(2), 1.),                 # A: began before the window
        (_h(2), _h(2, 5), 0.), (_h(2, 6), _h(2, 10), 0.),               # B: 60 s gap, one incident
        (_h(2, 10), _h(5), 1.),
        (_h(5), _h(5, 20), 0.), (_h(5, 20), _h(9), 1.),                 # C: completed
        (_h(9, 50), NOW, 0.),                                           # D: ongoing after a gap
    ]
    out = _sla(monkeypatch, segments)
    incidents = out["incidents"]
    assert len(incidents) == 4
    assert incidents[0]["starts_before_window"] is True and incidents[0]["duration_sec"] == 600
    assert incidents[1]["duration_sec"] == 600 and incidents[1]["downtime_sec"] == 540
    assert incidents[-1]["ongoing"] is True and incidents[-1]["end"] is None
    assert out["completed_incident_count"] == 2
    assert out["mttr_sec"] == pytest.approx((600 + 1200) / 2)
    assert out["mttr_population"] == "completed_in_window"
    # Observed up time between incidents; the 50-minute gap before D is unknown.
    assert out["mtbf_sec"] == pytest.approx((6600 + 10200 + 13200) / 3)
    assert out["ongoing_incident"]["start"] == _h(9, 50).isoformat()
    assert out["ongoing_incident"]["duration_sec"] == 600


def test_sla_budget_and_maintenance(monkeypatch):
    segments = [(S, _h(5), 1.), (_h(5), _h(5, 36), 0.), (_h(5, 36), NOW, 1.)]
    out = _sla(monkeypatch, segments, excluded=[(_h(5), _h(5, 18))], target=99.5)
    assert out["maintenance_sec"] == 18 * 60
    assert out["total_downtime_sec"] == 18 * 60
    assert out["covered_sec"] == 36000 - 18 * 60
    assert out["target_pct"] == 99.5 and out["target_source"] == "service"
    assert out["error_budget_sec"] == pytest.approx(0.005 * (36000 - 1080))
    assert out["budget_used_sec"] == 1080
    assert out["burn_rate"] == pytest.approx((1080 / (36000 - 1080)) / 0.005)
    assert out["meets_target"] is False
    policy = _sla(monkeypatch, [(S, NOW, 1.)])
    assert policy["target_source"] == "policy" and policy["meets_target"] is True


def test_incident_intervals_boundary_flags():
    segs = [(_h(0), _h(1), 0.), (_h(1), _h(2), 1.), (_h(3), _h(4), 0.)]
    hist = incident_intervals(segs, _h(0), _h(4), now=NOW)
    assert hist[0]["starts_before_window"] and hist[0]["duration_sec"] == 3600
    assert hist[1]["ends_after_window"] and not hist[1]["ongoing"] and hist[1]["end"] is None


# ── B06/B07/B08/B13: metrics ────────────────────────────────────────────────

class MetricsCH:
    def __init__(self, count=0, rows=()):
        self.count, self.rows, self.sql = count, list(rows), []

    def query(self, sql, parameters=None):
        self.sql.append(sql)
        if "SELECT count() FROM zenplus.service_metrics" in sql:
            return SimpleNamespace(result_rows=[(self.count,)])
        return SimpleNamespace(result_rows=self.rows)


def test_raw_truncation_keeps_newest_and_discloses_it():
    from app.services.service_metric_service import get_service_metrics
    newest_first = [(NOW - timedelta(seconds=10 * i), 50.0, 1, 200, None, None, None) for i in range(5000)]
    ch = MetricsCH(count=8640, rows=newest_first)
    out = get_service_metrics(uuid4(), NOW - timedelta(hours=24), NOW, "raw", client=ch, now=NOW)
    assert "ORDER BY timestamp DESC" in ch.sql[-1] and "LIMIT 5000" in ch.sql[-1]
    assert out.points[-1].timestamp == NOW and out.points[0].timestamp < out.points[-1].timestamp
    assert out.truncated is True and out.total_samples == 8640 and out.bucket_sec is None


def test_auto_granularity_by_expected_samples():
    from app.services.service_metric_service import choose_granularity
    assert choose_granularity("auto", timedelta(hours=24), 60) == "raw"      # 1440 probes
    assert choose_granularity("auto", timedelta(hours=24), 10) == "5m"       # 8640 probes
    assert choose_granularity("auto", timedelta(days=30), 60) == "1h"


def test_bucket_keeps_single_failure_visible_with_exact_p95():
    from app.services.service_metric_service import get_service_metrics
    row = (NOW - timedelta(minutes=5), 10, 1, 9, 120.0, 200.0, 250.0, "timeout", None)
    ch = MetricsCH(rows=[row])
    out = get_service_metrics(uuid4(), NOW - timedelta(days=2), NOW, "5m", client=ch, now=NOW)
    p = out.points[0]
    assert p.is_up is False and p.failed_count == 1 and p.sample_count == 10
    assert p.uptime_pct == pytest.approx(90) and p.p95_response_ms == 200 and p.error_message == "timeout"
    assert out.percentile_basis == "exact_per_bucket" and out.bucket_sec == 300
    assert "FROM zenplus.service_metrics\n" in ch.sql[-1] and "quantileIf(0.95)" in ch.sql[-1]


def test_rollup_fallback_is_sample_weighted():
    from app.services.service_metric_service import get_service_metrics
    # One 100 ms probe and nine 1000 ms probes in two batches: true mean 910 ms,
    # computed in SQL as sum(avg*count)/sum(count); here the row carries it.
    ch = MetricsCH(rows=[(NOW - timedelta(days=40), 10, 9.0, 910.0, 1000.0)])
    out = get_service_metrics(uuid4(), NOW - timedelta(days=45), NOW - timedelta(days=35), "1h", client=ch, now=NOW)
    p = out.points[0]
    assert "sum(avg_response_ms * sample_count) / nullIf(sum(sample_count), 0)" in ch.sql[-1]
    assert "avg(avg_response_ms)" not in ch.sql[-1]
    assert p.response_ms == 910 and p.uptime_pct == pytest.approx(90) and p.failed_count == 1
    assert p.is_up is False and p.p95_response_ms is None and out.percentile_basis == "unavailable"


def test_latest_probes_flags_manual_tests():
    from app.services.service_metric_service import get_latest_probes
    ch = MetricsCH(rows=[(NOW, 12.0, 1, 200, None, None, None, "api-test"),
                         (NOW - timedelta(minutes=1), 11.0, 1, 200, None, None, None, "controller")])
    out = get_latest_probes(uuid4(), 6, client=ch)
    assert [p.manual for p in out] == [True, False]
    assert "LIMIT 6" in ch.sql[-1]


# ── B14/B15: incidents ──────────────────────────────────────────────────────

def test_incident_entering_state_before_window():
    from app.services.service_incidents import build_incidents
    cid = uuid4()
    entering = (S - timedelta(hours=2), "down", "timeout")
    out = build_incidents(cid, entering, [(S + timedelta(minutes=30), "up", "ok")], S, NOW, NOW)
    assert len(out) == 1
    inc = out[0]
    assert inc["kind"] == "down" and inc["starts_before_window"] is True
    assert inc["start"] == (S - timedelta(hours=2)).isoformat()
    assert inc["duration_sec"] == 2.5 * 3600 and inc["in_window_sec"] == 1800
    assert inc["id"] == f"{cid}:{inc['start']}"


def test_warning_down_up_splits_and_unknown_is_a_gap():
    from app.services.service_incidents import build_incidents
    t = [(S + timedelta(minutes=10), "warning", "slow"), (S + timedelta(minutes=20), "down", "refused"),
         (S + timedelta(minutes=30), "up", "ok"), (S + timedelta(minutes=40), "degraded", "slow"),
         (S + timedelta(minutes=50), "unknown", "sensor offline"), (S + timedelta(minutes=70), "up", "ok")]
    out = build_incidents(uuid4(), (S - timedelta(days=1), "up", ""), t, S, NOW, NOW)
    kinds = [(i["kind"], i["duration_sec"]) for i in out]
    assert kinds == [("warning", 600), ("down", 600), ("warning", 600)]
    assert out[0]["severity_path"] == ["warning", "down"] == out[1]["severity_path"]
    assert out[2]["reason_end"] == "sensor offline"  # closed by the gap, not drawn as warning


def test_ongoing_and_historical_open_incidents():
    from app.services.service_incidents import build_incidents
    t = [(S + timedelta(minutes=10), "down", "x")]
    now_inc = build_incidents(uuid4(), None, t, S, NOW, NOW)[0]
    assert now_inc["ongoing"] is True and now_inc["end"] is None
    end = S + timedelta(hours=1)
    closing = (end + timedelta(minutes=5), "up", "ok")
    hist = build_incidents(uuid4(), None, t, S, end, NOW, closing)[0]
    assert hist["ongoing"] is False and hist["end"] == closing[0].isoformat()
    assert hist["ends_after_window"] is True and hist["in_window_sec"] == 50 * 60


def test_transitions_are_paged_past_fixed_caps(monkeypatch):
    from app.services import service_incidents
    monkeypatch.setattr(service_incidents, "PAGE", 3)
    events = [(S + timedelta(seconds=i), "down" if i % 2 else "up", "") for i in range(8)]

    class CH:
        def query(self, sql, parameters=None):
            if "timestamp < %(f)s" in sql and "DESC" in sql:
                return SimpleNamespace(result_rows=[(S - timedelta(hours=1), "up", "")])
            cursor = datetime.strptime(parameters["c"], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
            op_ge = ">= %(c)s" in sql
            rows = [e for e in events if (e[0] >= cursor if op_ge else e[0] > cursor)]
            return SimpleNamespace(result_rows=rows[:3])
    entering, transitions = service_incidents.read_transitions(CH(), uuid4(), S, NOW)
    assert entering[1] == "up" and len(transitions) == 8


# ── Hourly / daily ──────────────────────────────────────────────────────────

def test_legacy_daily_uptime_is_null_without_samples(monkeypatch):
    from app.core import database
    cid = uuid4()

    class DB:
        async def execute(self, stmt, params=None):
            return Res([(cid, None, 60, None, ["team-a"])])

    class CH:
        def query(self, sql, parameters=None):
            if "service_metrics_5m" in sql:
                return SimpleNamespace(result_rows=[])
            if "argMax" in sql:
                return SimpleNamespace(result_rows=[(cid, "up")])
            return SimpleNamespace(result_rows=[])

    async def maint(db, checks, start, end):
        return {}
    monkeypatch.setattr(database, "get_clickhouse_client", lambda: CH())
    monkeypatch.setattr(service_maintenance, "intervals_for_checks", maint)
    out = asyncio.run(service_check_service.get_daily_uptime_all(DB(), 2, visible_tags=["team-a"]))
    rows = out["checks"][str(cid)]
    assert rows and all(r["uptime_pct"] is None and r["sample_count"] == 0 for r in rows)
    assert all(r["status_uptime_pct"] == 100 for r in rows)


# ── List / summary ──────────────────────────────────────────────────────────

class RecordingDB(CheckDB):
    async def execute(self, stmt, params=None):
        self.statements.append(stmt)
        return Res()


def test_list_supports_status_sets_filters_and_triage_sort():
    db = RecordingDB()
    out = asyncio.run(service_check_service.get_service_checks(
        db, status="warning,degraded", sort="status", stale=True, enabled=True, has_credential=False,
        in_maintenance=False, sensor_id=uuid4(), visible_tags=["team-a"]))
    compiled = [str(s.compile(compile_kwargs={})) for s in db.statements]
    page = next(c for c in compiled if "ORDER BY" in c and "LIMIT" in c)
    assert "service_checks.status IN" in page and "CASE" in page.split("ORDER BY")[1]
    assert "make_interval" in page and "sensor_assignments" in page
    assert "credential_id IS NULL" in page
    assert out["meta"]["server_time"] and out["meta"]["sort"] == "status"


def test_freshness_uses_effective_interval():
    up = check("a", last_check_at=NOW - timedelta(seconds=300), status="up")
    assert service_check_service.evidence_freshness(up, NOW) == (True, 300)   # > 3*60+10+60
    fresh = check("b", last_check_at=NOW - timedelta(seconds=200), status="up")
    assert service_check_service.evidence_freshness(fresh, NOW)[0] is False
    down = check("c", last_check_at=NOW - timedelta(seconds=200), status="down")
    assert service_check_service.evidence_freshness(down, NOW)[0] is True    # 3*30+10+60 = 160
    paused = check("d", last_check_at=None, enabled=False)
    assert service_check_service.evidence_freshness(paused, NOW) == (False, None)
    never = check("e", last_check_at=None)
    assert service_check_service.evidence_freshness(never, NOW) == (True, None)


def test_summary_adds_paused_stale_and_maintenance(monkeypatch):
    calls = []

    class DB:
        async def execute(self, stmt, params=None):
            calls.append(stmt)
            if len(calls) == 1:
                return Res([("up", 3), ("warning", 1), ("degraded", 1)])
            if len(calls) == 2:
                return Res([(2, 1)])
            return Res([1])

    async def active(db):
        return {uuid4()}
    monkeypatch.setattr(service_check_service, "active_maintenance_check_ids", active)
    out = asyncio.run(service_check_service.get_service_check_summary(DB(), visible_tags=["team-a"]))
    assert (out.total, out.warning, out.degraded, out.paused, out.stale, out.in_maintenance) == (5, 1, 1, 2, 1, 1)
    assert out.server_time is not None


# ── Export / import ─────────────────────────────────────────────────────────

def test_portable_export_redacts_secrets():
    sc = check("web", http_headers={"Authorization": "Bearer abc", "X-Api-Key": "k", "Accept": "json"},
               http_body="user=a&password=b",
               workflow_steps=[{"name": "login", "url": "https://h/login", "method": "POST",
                                "headers": {"Cookie": "sid=1"}, "body": "p={{password}}",
                                "expected_statuses": "200", "follow_redirects": True}])
    doc = asyncio.run(service_check_service.export_portable(CheckDB([sc])))
    item = doc["checks"][0]
    assert doc["schema_version"] == 2 and set(item) == set(service_check_service.PORTABLE_FIELDS)
    assert item["http_headers"] == {"Authorization": "__REDACTED__", "X-Api-Key": "__REDACTED__", "Accept": "json"}
    assert item["http_body"] == "__REDACTED__"
    assert item["workflow_steps"][0]["headers"] == {"Cookie": "__REDACTED__"}
    assert item["workflow_steps"][0]["body"] == "__REDACTED__"
    for key in ("retry_count", "retry_delay_s", "down_check_interval", "level", "tags", "config"):
        assert key in item
    assert "abc" not in str(doc) and "password=b" not in str(doc)


def test_import_dry_run_resolves_names_and_never_imports_secrets(monkeypatch):
    from app.services import service_reports
    gid, cred = uuid4(), uuid4()
    created = []

    class DB(CheckDB):
        async def execute(self, stmt, params=None):
            sql = str(stmt)
            if sql.startswith("SELECT service_checks.name, service_checks.id"):
                return Res([("Existing", uuid4())])
            if sql.startswith("SELECT service_checks.name, service_checks.tags"):
                return Res([("Existing", ["team-a"])])
            if "FROM service_check_groups" in sql:
                return Res([("Web", gid)])
            if "FROM service_credentials" in sql:
                return Res([("api-cred", cred)])
            return Res()

    async def fake_create(db, data, user_id, visible_tags=None, commit=True):
        created.append(data)
        return SimpleNamespace(id=uuid4(), parent_check_id=None, name=data.name)
    monkeypatch.setattr(service_check_service, "create_service_check", fake_create)
    doc = {"schema_version": 2, "checks": [
        {"name": "Existing", "check_type": "tcp", "target_host": "h"},
        {"name": "New HTTP", "check_type": "http", "target_host": "h", "target_url": "https://h/",
         "http_headers": {"Authorization": "__REDACTED__", "Accept": "json"}, "group_name": "Web",
         "credential_name": "api-cred", "parent_check_name": "New TCP", "level": 3, "tags": ["team-a"]},
        {"name": "New TCP", "check_type": "tcp", "target_host": "h", "target_port": 443, "tags": ["team-a"]},
        {"name": "Bad", "check_type": "http", "target_host": "h", "credential_name": "missing"},
    ]}
    out = asyncio.run(service_reports.import_checks(DB(), doc, SimpleNamespace(id=uuid4()), ["team-a"], dry_run=True))
    assert out["summary"] == {"create": 2, "skip_existing": 1, "error": 1}
    http = next(d for d in created if d.name == "New HTTP")
    assert http.http_headers == {"Accept": "json"} and http.enabled is False
    assert http.group_id == gid and http.credential_id == cred
    entry = next(r for r in out["results"] if r["name"] == "New HTTP")
    assert any("redacted" in w for w in entry["warnings"])


def test_import_rejects_unknown_document():
    from app.services import service_reports
    with pytest.raises(service_check_service.ServiceCheckValidationError):
        asyncio.run(service_reports.import_checks(CheckDB(), [{"name": "x"}], None))


# ── Alerts bulk acknowledge ─────────────────────────────────────────────────

def test_bulk_acknowledge_is_scoped_and_requires_a_filter(api):
    # Rebuilt by the Alerts review on alert_workflow; the filter it builds
    # must carry the caller's scope and only select active alerts.
    from app.services import alert_service
    r = api["client"].post("/api/v1/alerts/acknowledge-bulk", json={})
    assert r.status_code == 422
    f = alert_service.AlertFilters(status="active", service_check_id=uuid4(),
                                   visible_tags=["team-a"])
    clauses, params = f.where()
    assert any("service_checks _vc" in c for c in clauses)
    assert "a.status = :status" in clauses and params["status"] == "active"
    assert params["vis_tags"] == ["team-a"]


# ── Fleet cache ─────────────────────────────────────────────────────────────

def test_fleet_cache_serves_repeat_requests(monkeypatch):
    from app.core import database
    from app.services import service_availability
    service_check_service.clear_fleet_cache()
    scans = []

    class DB:
        async def execute(self, stmt, params=None):
            return Res([("a", None, 60, None, ["team-a"])])

    def read(ch, checks, start, end):
        scans.append(1)
        yield "a", [(start, end, 1)], 1, start

    async def maint(db, checks, start, end):
        return {}
    monkeypatch.setattr(database, "get_clickhouse_client", lambda: None)
    monkeypatch.setattr(service_availability, "read_fleet_segments", read)
    monkeypatch.setattr(service_maintenance, "intervals_for_checks", maint)
    first = asyncio.run(service_check_service.get_confirmed_uptime_all(DB(), visible_tags=["team-a"]))
    second = asyncio.run(service_check_service.get_confirmed_uptime_all(DB(), visible_tags=["team-a"]))
    other = asyncio.run(service_check_service.get_confirmed_uptime_all(DB(), visible_tags=["team-b"]))
    assert first is second and len(scans) == 2 and other["checks"] == {"a": 100}
    service_check_service.clear_fleet_cache()


def test_fleet_report_route_is_not_shadowed_by_check_id(api):
    api["db"] = CheckDB([])
    r = api["client"].get("/api/v1/service-checks/report", params={"format": "csv", "timezone": "Asia/Riyadh"})
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers["content-disposition"]
    assert r.text.startswith("# Services availability report")
    bad = api["client"].get("/api/v1/service-checks/report", params={"timezone": "Mars/Base"})
    assert bad.status_code == 422


@pytest.mark.parametrize("path", ["acknowledge", "resolve", "snooze", "unsnooze"])
def test_single_alert_mutations_are_scoped(api, monkeypatch, path):
    from app.core import security
    from app.services import alert_workflow
    touched = []

    async def out_of_scope(db, alert_id, scope):
        return False

    async def mutate(*a, **k):
        touched.append(path)
        return True

    async def perms(db, role):
        return ["alerts.view", "alerts.acknowledge"]

    monkeypatch.setattr(scoping, "alert_in_scope", out_of_scope)
    monkeypatch.setattr(security, "get_role_permissions", perms)
    for name in ("acknowledge", "resolve", "snooze", "unsnooze"):
        monkeypatch.setattr(alert_workflow, name, mutate)
    body = {"minutes": 30} if path == "snooze" else None
    r = api["client"].post(f"/api/v1/alerts/{uuid4()}/{path}", json=body)
    assert r.status_code == 404 and not touched
