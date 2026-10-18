"""Report schedule API: report types (catalog keys, legacy types, 'custom'
with a template the caller may read), calendar periods, per-type formats,
direct recipients, NetFlow scope preservation on update, audit actions and
the listing's derived fields.

Runs against an in-memory double; the app's startup hooks are not run (the
client is not used as a context manager), so nothing touches a database.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import report_schedules as RS
from app.core import scoping, security
from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app

ROLES = {
    "rsx_admin": ["system.admin"],
    "rsx_manager": ["reports.view", "reports.manage"],
    "rsx_viewer": ["reports.view"],
}
OWNER_NAMES: dict[str, str] = {}


def _user(role: str, name: str | None = None, scope_tags=None):
    uid = uuid.uuid4()
    OWNER_NAMES[str(uid)] = name or f"{role} user"
    return SimpleNamespace(id=uid, username=role, full_name=name or f"{role} user", role=role,
                           is_active=True, scope_tags=scope_tags or [])


class _Null:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Res:
    def __init__(self, rows=None, scalar=None):
        self.rows = rows or []
        self._scalar = scalar

    def first(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def scalar(self):
        return self._scalar


JSONB = ("filters", "notify_channels", "recipients")
PARAM_COLUMNS = {"n": "next_run_at", "e": "enabled", "st": "last_status", "err": "last_error"}


class ScheduleDB:
    """Answers the statements report_schedules.py issues, by SQL shape."""

    def __init__(self):
        self.schedules: dict[str, dict] = {}
        self.templates: dict[str, SimpleNamespace] = {}
        self.audits: list[dict] = []
        self.statements: list[str] = []

    def begin_nested(self):
        return _Null()

    async def commit(self):
        return None

    async def rollback(self):
        return None

    def add_schedule(self, **kw) -> str:
        sid = str(uuid.uuid4())
        row = dict(id=uuid.UUID(sid), name="Weekly", description=None, enabled=True,
                   report_type="availability", period="last_7d", format="pdf", filters={},
                   frequency="weekly", hour=8, minute=0, day_of_week=1, day_of_month=None,
                   notify_channels=[], recipients=[], custom_report_id=None, last_run_at=None,
                   last_status=None, last_error=None,
                   next_run_at=datetime(2026, 10, 12, 5, tzinfo=timezone.utc), created_by=None,
                   created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                   updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        row.update(kw)
        self.schedules[sid] = row
        return sid

    def add_template(self, *, owner, visibility="shared", name="Team SLA") -> str:
        tid = str(uuid.uuid4())
        self.templates[tid] = SimpleNamespace(
            id=tid, name=name, description="", sections=["availability_kpis"],
            definition={"version": 1, "sections": [{"id": "availability_kpis"}]}, category="availability",
            tags=[], visibility=visibility, source_key=None, default_period=None,
            owner_id=str(owner.id) if owner else None, owner_name=None, created_at=None, updated_at=None,
            schedules=0)
        return tid

    def _detail(self, row: dict) -> SimpleNamespace:
        t = self.templates.get(str(row.get("custom_report_id") or ""))
        return SimpleNamespace(
            **row, owner_name=OWNER_NAMES.get(str(row.get("created_by") or "")),
            template_name=t.name if t else None, template_visibility=t.visibility if t else None,
            template_owner_id=t.owner_id if t else None)

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        params = dict(params or {})
        self.statements.append(sql)
        if sql.startswith("INSERT INTO audit_logs"):
            self.audits.append({**params, "metadata": json.loads(params["metadata"])})
            return _Res()
        if "FROM custom_reports c" in sql:
            t = self.templates.get(str(params.get("id")))
            return _Res([t] if t else [])
        if sql.startswith("INSERT INTO report_schedules"):
            sid = str(uuid.uuid4())
            row = {k: (json.loads(v) if k in JSONB else v) for k, v in params.items()}
            row.update(id=uuid.UUID(sid), last_run_at=None, last_status=None, last_error=None,
                       created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
            self.schedules[sid] = row
            return _Res([SimpleNamespace(**row)])
        if sql.startswith("SELECT s.*"):
            if "WHERE s.id" in sql:
                row = self.schedules.get(str(params["id"]))
                return _Res([self._detail(row)] if row else [])
            return _Res([self._detail(r) for r in self.schedules.values()])
        if sql.startswith("SELECT * FROM report_schedules WHERE id"):
            row = self.schedules.get(str(params["id"]))
            return _Res([SimpleNamespace(**row)] if row else [])
        if sql.startswith("UPDATE report_schedules SET"):
            row = self.schedules.get(str(params["id"]))
            if row is None:
                return _Res()
            for key, value in params.items():
                if key == "id":
                    continue
                col = PARAM_COLUMNS.get(key, key)
                row[col] = json.loads(value) if col in JSONB else value
            return _Res([SimpleNamespace(**row)])
        if sql.startswith("DELETE FROM report_schedules"):
            row = self.schedules.pop(str(params["id"]), None)
            return _Res([SimpleNamespace(**row)] if row else [])
        raise AssertionError(f"unexpected SQL: {sql[:120]}")


class Vis:
    def __init__(self, netflow: bool, exporters=None):
        self.permissions = frozenset({"netflow.view"} if netflow else set())
        self.exporters = exporters

    def can(self, perm):
        return perm in self.permissions

    def allows(self, exporter):
        return self.exporters is None or exporter in self.exporters


@pytest.fixture
def env(monkeypatch):
    db = ScheduleDB()
    state = {"user": _user("rsx_admin"), "netflow": True}

    async def role_perms(_db, role):
        return list(ROLES.get(role, []))

    async def no_role_scope(_db, role):
        return []

    async def fake_visibility(_db, user):
        return Vis(state["netflow"])

    async def no_load(*a, **k):
        return None

    monkeypatch.setattr(security, "get_role_permissions", role_perms)
    monkeypatch.setattr(RS, "get_role_permissions", role_perms)
    monkeypatch.setattr(scoping, "get_role_permissions", role_perms)
    monkeypatch.setattr(scoping, "get_role_scope_tags", no_role_scope)
    from app.api.v1.netflow import access
    from app.services import netflow_classify, netflow_groups
    monkeypatch.setattr(access, "visibility_for_user", fake_visibility)
    # NetFlow scope validation loads classifier rules / IP groups through its
    # own sessions: never from a test.
    monkeypatch.setattr(netflow_classify, "load_rules", no_load)
    monkeypatch.setattr(netflow_groups, "load_groups", no_load)

    async def tz(_db):
        return "Asia/Riyadh"

    monkeypatch.setattr(RS, "get_configured_timezone", tz)

    async def fake_db():
        yield db

    async def current_user():
        return state["user"]

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = current_user
    client = TestClient(app)  # no context manager: startup hooks (and their DB access) are not run
    yield SimpleNamespace(db=db, state=state, client=client)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def _post(env, body):
    return env.client.post("/api/v1/report-schedules", json=body)


def _put(env, sid, body):
    return env.client.put(f"/api/v1/report-schedules/{sid}", json=body)


# ── Validation ───────────────────────────────────────────────────────────────

def test_catalog_keys_legacy_types_and_calendar_periods_are_accepted(env):
    for body in (
        {"name": "Exec", "report_type": "executive_summary", "period": "last_24h", "format": "csv"},
        {"name": "SLA", "report_type": "availability", "period": "previous_month", "format": "html"},
        {"name": "Ops", "report_type": "executive_overview", "period": "week_to_date", "format": "excel"},
        {"name": "Q", "report_type": "inventory", "period": "previous_quarter", "format": "none"},
    ):
        r = _post(env, body)
        assert r.status_code == 201, (body, r.text)
        assert r.json()["period"] == body["period"] and r.json()["format"] == body["format"]


@pytest.mark.parametrize("body", [
    {"report_type": "no_such_template"},
    {"report_type": "Availability"},
    {"period": "custom"},
    {"period": "last_year"},
    {"format": "docx"},
    {"recipients": ["not-an-address"]},
    {"recipients": ["ops@example.com\r\nBcc: x@evil.test"]},
    {"recipients": [f"user{i}@example.com" for i in range(51)]},
])
def test_invalid_fields_are_rejected(env, body):
    r = _post(env, {"name": "Bad", **body})
    assert r.status_code == 422, r.text
    assert not env.db.schedules


@pytest.mark.parametrize("report_type,fmt", [
    ("executive_summary", "html"),   # legacy: pdf | excel | csv | none
    ("availability", "csv"),         # templates: pdf | excel | html | none
])
def test_format_must_suit_the_report_type(env, report_type, fmt):
    r = _post(env, {"name": "Bad", "report_type": report_type, "format": fmt})
    assert r.status_code == 400 and "delivered as" in r.json()["detail"]


def test_recipients_are_lowercased_deduplicated_and_stored(env):
    r = _post(env, {"name": "Mail", "report_type": "availability",
                    "recipients": ["Ops@Example.com", "ops@example.com", " cto@example.org "]})
    assert r.status_code == 201, r.text
    assert r.json()["recipients"] == ["ops@example.com", "cto@example.org"]
    stored = next(iter(env.db.schedules.values()))
    assert stored["recipients"] == ["ops@example.com", "cto@example.org"]
    # Exactly 50 distinct addresses are fine (duplicates do not count).
    many = [f"user{i}@example.com" for i in range(50)] + ["USER1@example.com"]
    assert _post(env, {"name": "Many", "report_type": "availability", "recipients": many}).status_code == 201


def test_custom_needs_a_template_the_caller_can_read(env):
    me = env.state["user"] = _user("rsx_manager")
    other = _user("rsx_manager")
    shared = env.db.add_template(owner=other, visibility="shared", name="Shared SLA")
    private_other = env.db.add_template(owner=other, visibility="private", name="Secret")
    private_mine = env.db.add_template(owner=me, visibility="private", name="Mine")

    assert _post(env, {"name": "c", "report_type": "custom"}).status_code == 400
    assert _post(env, {"name": "c", "report_type": "custom", "custom_report_id": private_other}).status_code == 404
    assert _post(env, {"name": "c", "report_type": "custom", "custom_report_id": str(uuid.uuid4())}).status_code == 404
    assert _post(env, {"name": "c", "report_type": "custom", "custom_report_id": "not-a-uuid"}).status_code == 404
    for tid in (shared, private_mine):
        r = _post(env, {"name": "c", "report_type": "custom", "custom_report_id": tid})
        assert r.status_code == 201 and r.json()["custom_report_id"] == tid
    # A full administrator may deliver anyone's template.
    env.state["user"] = _user("rsx_admin")
    assert _post(env, {"name": "c", "report_type": "custom", "custom_report_id": private_other}).status_code == 201
    # A built-in type never keeps a stray template id.
    r = _post(env, {"name": "b", "report_type": "availability", "custom_report_id": shared})
    assert r.status_code == 201 and r.json()["custom_report_id"] is None


def test_update_validates_the_merged_schedule(env):
    me = env.state["user"] = _user("rsx_manager")
    other = _user("rsx_manager")
    secret = env.db.add_template(owner=other, visibility="private")
    shared = env.db.add_template(owner=other, visibility="shared")
    sid = env.db.add_schedule(report_type="executive_summary", format="csv", created_by=me.id)

    assert _put(env, sid, {"report_type": "availability"}).status_code == 400      # stored csv
    assert _put(env, sid, {"report_type": "availability", "format": "html"}).status_code == 200
    assert _put(env, sid, {"report_type": "custom", "custom_report_id": secret}).status_code == 404
    assert _put(env, sid, {"report_type": "custom"}).status_code == 400
    r = _put(env, sid, {"report_type": "custom", "custom_report_id": shared})
    assert r.status_code == 200 and r.json()["custom_report_id"] == shared
    # Unrelated edits do not re-check the template; switching away clears it.
    assert _put(env, sid, {"name": "Renamed"}).status_code == 200
    r = _put(env, sid, {"report_type": "inventory", "custom_report_id": None})
    assert r.status_code == 200 and r.json()["custom_report_id"] is None
    assert _put(env, sid, {"period": None}).status_code == 400
    assert _put(env, sid, {"recipients": ["A@B.example"]}).json()["recipients"] == ["a@b.example"]
    assert _put(env, str(uuid.uuid4()), {"name": "x"}).status_code == 404


def test_writes_need_reports_manage_outside_a_visibility_scope(env, monkeypatch):
    env.state["user"] = _user("rsx_viewer")
    assert _post(env, {"name": "x", "report_type": "availability"}).status_code == 403
    env.state["user"] = _user("rsx_manager")

    async def scoped(_db, user):
        return ["team-a"]

    monkeypatch.setattr(scoping, "visible_tags", scoped)
    assert _post(env, {"name": "x", "report_type": "availability"}).status_code == 403
    assert not env.db.schedules


# ── NetFlow scope preservation ───────────────────────────────────────────────

def test_update_keeps_a_netflow_scope_the_caller_cannot_see(env):
    scope = {"exporter": "10.9.0.1", "talker": "172.31.9.10"}
    dev = str(uuid.uuid4())
    sid = env.db.add_schedule(report_type="traffic", filters={"netflow": scope, "device_ids": [dev]})
    env.state["user"] = _user("rsx_manager")
    env.state["netflow"] = False   # no netflow.view

    listing = env.client.get("/api/v1/report-schedules").json()["data"][0]
    assert listing["filters"] == {"device_ids": [dev], "netflow_withheld": True}

    # The editor sends back what it was given: the stored scope survives.
    r = _put(env, sid, {"name": "Edited", "filters": listing["filters"]})
    assert r.status_code == 200, r.text
    assert env.db.schedules[sid]["filters"] == {"device_ids": [dev], "netflow": scope}
    # Nor can it be replaced or cleared by a caller who cannot see it.
    _put(env, sid, {"filters": {"netflow": {"exporter": "10.9.0.2"}}})
    _put(env, sid, {"filters": {}})
    assert env.db.schedules[sid]["filters"] == {"netflow": scope}

    # A caller who can see it may change or clear it; the marker is never stored.
    env.state["netflow"] = True
    _put(env, sid, {"filters": {"netflow_withheld": True}})
    assert env.db.schedules[sid]["filters"] == {}


def test_device_filter_must_be_uuids(env):
    r = _post(env, {"name": "x", "report_type": "availability", "filters": {"device_ids": ["1; drop"]}})
    assert r.status_code == 400


# ── Audit ────────────────────────────────────────────────────────────────────

def test_every_change_is_audited(env, monkeypatch):
    async def fake_run(db, sched, **kw):
        assert sched["recipients"] == ["ops@example.com"]
        return {"run_id": "r1", "token": "t", "view_url": None, "delivered": ["ops@example.com"],
                "errors": [], "status": "success"}

    monkeypatch.setattr(RS, "generate_and_deliver", fake_run)
    r = _post(env, {"name": "Audited", "report_type": "availability", "period": "previous_week",
                    "format": "pdf", "recipients": ["ops@example.com"]})
    sid = r.json()["id"]
    assert _put(env, sid, {"period": "previous_month"}).status_code == 200
    assert env.client.post(f"/api/v1/report-schedules/{sid}/toggle").status_code == 200
    run = env.client.post(f"/api/v1/report-schedules/{sid}/run-now").json()
    assert run["status"] == "success" and run["message"] == "Report generated (success)"
    assert env.client.delete(f"/api/v1/report-schedules/{sid}").status_code == 204

    actions = [a["action"] for a in env.db.audits]
    assert actions == ["reports.schedule.create", "reports.schedule.update", "reports.schedule.toggle",
                       "reports.schedule.run_now", "reports.schedule.delete"]
    for a in env.db.audits:
        assert a["resource_type"] == "report_schedule" and a["resource_id"] == sid
        meta = a["metadata"]
        assert meta["name"] == "Audited" and meta["report_type"] == "availability"
        assert meta["format"] == "pdf" and meta["recipients"] == 1
    assert env.db.audits[0]["metadata"]["period"] == "previous_week"
    assert env.db.audits[1]["metadata"]["period"] == "previous_month"
    assert env.db.audits[1]["metadata"]["changed"] == ["period"]
    assert env.db.audits[2]["metadata"]["enabled"] is False
    assert env.db.audits[3]["metadata"]["status"] == "success"


def test_run_now_reports_a_schedule_that_could_not_be_generated(env, monkeypatch):
    async def gone(db, sched, **kw):
        return {"run_id": None, "token": None, "view_url": None, "delivered": [],
                "errors": ["The organisation report template this schedule delivers was deleted"],
                "status": "failed"}

    monkeypatch.setattr(RS, "generate_and_deliver", gone)
    sid = env.db.add_schedule()
    body = env.client.post(f"/api/v1/report-schedules/{sid}/run-now").json()
    assert body["status"] == "failed" and body["message"].startswith("Report not generated: ")
    assert env.db.schedules[sid]["last_status"] == "failed"


# ── Listing ──────────────────────────────────────────────────────────────────

def test_listing_fields(env):
    owner = _user("rsx_manager", name="Nadia Ops")
    someone = _user("rsx_manager")
    shared = env.db.add_template(owner=someone, visibility="shared", name="Shared SLA")
    secret = env.db.add_template(owner=someone, visibility="private", name="Board pack")
    ids = {
        "preset": env.db.add_schedule(report_type="availability", created_by=owner.id,
                                      recipients=["ops@example.com"]),
        "legacy": env.db.add_schedule(report_type="executive_summary", format="csv"),
        "custom": env.db.add_schedule(report_type="custom", custom_report_id=shared),
        "secret": env.db.add_schedule(report_type="custom", custom_report_id=secret),
        "gone": env.db.add_schedule(report_type="custom", custom_report_id=None),
    }

    env.state["user"] = _user("rsx_viewer")
    rows = {r["id"]: r for r in env.client.get("/api/v1/report-schedules").json()["data"]}
    preset = rows[ids["preset"]]
    assert preset["report_title"] == "Availability & SLA Report"
    assert preset["owner_name"] == "Nadia Ops" and preset["owner_id"] == str(owner.id)
    assert preset["recipients"] == ["ops@example.com"]
    assert preset["next_run_at"] == "2026-10-12T05:00:00+00:00"
    assert preset["can_edit"] is False                      # a viewer cannot change schedules
    assert rows[ids["legacy"]]["report_title"] == "Executive Summary"
    assert rows[ids["custom"]]["report_title"] == "Shared SLA"
    assert rows[ids["secret"]]["report_title"] == "Private template"
    assert rows[ids["gone"]]["report_title"] == "Deleted template"

    env.state["user"] = _user("rsx_manager")
    assert all(r["can_edit"] for r in env.client.get("/api/v1/report-schedules").json()["data"])
    env.state["user"] = someone
    rows = {r["id"]: r for r in env.client.get("/api/v1/report-schedules").json()["data"]}
    assert rows[ids["secret"]]["report_title"] == "Board pack"   # its owner sees the name

    one = env.client.get(f"/api/v1/report-schedules/{ids['preset']}")
    assert one.status_code == 200 and one.json()["report_title"] == "Availability & SLA Report"
    assert env.client.get(f"/api/v1/report-schedules/{uuid.uuid4()}").status_code == 404


def test_reads_need_reports_view_explicitly():
    for path in ("/report-schedules", "/report-schedules/{schedule_id}", "/report-schedules/{schedule_id}/runs"):
        route = next(r for r in RS.router.routes if r.path == path and "GET" in r.methods)
        names = [getattr(d.call, "__qualname__", "") for d in route.dependant.dependencies]
        assert any("require_permission" in n for n in names), (path, names)


def test_report_type_vocabulary_matches_the_catalog():
    from app.services import report_sections

    for key in report_sections.REPORT_PRESETS:
        assert RS.is_known_report_type(key), key
    for key in ("executive_summary", "device_health", "service_health", "alert_analysis", "full_report",
                "custom"):
        assert RS.is_known_report_type(key)
    assert not RS.is_known_report_type("custom_report")
    from app.services import report_periods, report_templates
    assert tuple(report_templates.PERIODS) == report_periods.PERIOD_IDS



def test_a_manager_who_changes_someone_elses_delivery_takes_the_schedule_over(env):
    """A schedule renders with its owner's permissions: a report manager who
    changes what it sends, or to whom, must not borrow the owner's access."""
    admin = env.state["user"]
    sid = _post(env, {"name": "Board pack", "report_type": "availability", "period": "previous_month",
                      "format": "pdf", "recipients": ["cio@example.com"]}).json()["id"]
    assert str(env.db.schedules[sid]["created_by"]) == str(admin.id)

    manager = _user("rsx_manager", "Report Manager")
    env.state["user"] = manager
    # Pausing or re-timing does not change what is delivered: ownership stays.
    assert env.client.post(f"/api/v1/report-schedules/{sid}/toggle").status_code == 200
    assert _put(env, sid, {"hour": 9}).status_code == 200
    assert str(env.db.schedules[sid]["created_by"]) == str(admin.id)

    # Redirecting it to new recipients hands it to the editor.
    r = _put(env, sid, {"recipients": ["me@outside.example"]})
    assert r.status_code == 200, r.text
    assert str(env.db.schedules[sid]["created_by"]) == str(manager.id)
    audit = env.db.audits[-1]
    assert audit["action"] == "reports.schedule.update"
    assert audit["metadata"]["owner_changed_from"] == str(admin.id)
    assert "created_by" not in audit["metadata"]["changed"]

    # A full administrator editing keeps the existing owner.
    env.state["user"] = admin
    assert _put(env, sid, {"report_type": "alerts"}).status_code == 200
    assert str(env.db.schedules[sid]["created_by"]) == str(manager.id)


# ── Timezone (NOTIFY 07) ─────────────────────────────────────────────────────

def test_a_new_schedule_keeps_the_organisation_zone_and_returns_local_and_utc(env):
    r = _post(env, {"name": "NOC Daily Briefing", "report_type": "availability", "frequency": "daily",
                    "hour": 8, "minute": 0})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["timezone"] == "Asia/Riyadh" and body["effective_timezone"] == "Asia/Riyadh"
    assert body["organisation_timezone"] == "Asia/Riyadh"
    assert body["local_time_label"] == "08:00 Asia/Riyadh (05:00 UTC)"
    assert body["schedule_label"] == "Daily at 08:00 Asia/Riyadh (05:00 UTC)"
    nxt = datetime.fromisoformat(body["next_run_at"])
    assert (nxt.hour, nxt.minute, nxt.utcoffset()) == (5, 0, timedelta(0))
    assert body["next_run_local"].endswith("T08:00:00+03:00")
    assert next(iter(env.db.schedules.values()))["timezone"] == "Asia/Riyadh"


def test_weekly_sunday_in_a_chosen_zone(env):
    r = _post(env, {"name": "Weekly", "report_type": "availability", "frequency": "weekly", "day_of_week": 7,
                    "hour": 8, "minute": 0, "timezone": " Europe/Berlin "})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["timezone"] == "Europe/Berlin"
    nxt = datetime.fromisoformat(body["next_run_at"])
    local = nxt.astimezone(ZoneInfo("Europe/Berlin"))
    assert local.isoweekday() == 7 and (local.hour, local.minute) == (8, 0)
    assert body["local_time_label"].startswith("08:00 Europe/Berlin (0")


@pytest.mark.parametrize("tz", ["Mars/Olympus", "GMT+3 Riyadh", "x" * 70])
def test_an_unknown_zone_is_rejected(env, tz):
    r = _post(env, {"name": "Bad", "report_type": "availability", "timezone": tz})
    assert r.status_code == 422, r.text
    assert not env.db.schedules


def test_an_organisation_zone_change_does_not_move_a_schedule(env, monkeypatch):
    sid = _post(env, {"name": "Daily", "report_type": "availability", "hour": 8}).json()["id"]
    before = env.client.get(f"/api/v1/report-schedules/{sid}").json()

    async def utc(_db):
        return "UTC"

    monkeypatch.setattr(RS, "get_configured_timezone", utc)
    after = env.client.get(f"/api/v1/report-schedules/{sid}").json()
    assert after["next_run_at"] == before["next_run_at"]
    assert after["local_time_label"] == "08:00 Asia/Riyadh (05:00 UTC)"
    # Re-timing and toggling compute in the schedule's own zone.
    moved = _put(env, sid, {"hour": 9}).json()
    assert datetime.fromisoformat(moved["next_run_at"]).hour == 6
    env.client.post(f"/api/v1/report-schedules/{sid}/toggle")
    resumed = env.client.post(f"/api/v1/report-schedules/{sid}/toggle").json()
    assert datetime.fromisoformat(resumed["next_run_at"]).hour == 6
    # Changing the zone recomputes; clearing it pins the organisation zone of the moment.
    tokyo = _put(env, sid, {"timezone": "Asia/Tokyo"}).json()
    assert tokyo["timezone"] == "Asia/Tokyo" and datetime.fromisoformat(tokyo["next_run_at"]).hour == 0
    cleared = _put(env, sid, {"timezone": None}).json()
    assert cleared["timezone"] == "UTC" and datetime.fromisoformat(cleared["next_run_at"]).hour == 9


def test_rows_from_before_the_migration_fall_back_to_the_organisation_zone(env):
    sid = env.db.add_schedule()                    # no timezone key at all
    row = env.client.get(f"/api/v1/report-schedules/{sid}").json()
    assert row["timezone"] is None and row["effective_timezone"] == "Asia/Riyadh"
    assert row["local_time_label"] == "08:00 Asia/Riyadh (05:00 UTC)"


def test_a_time_in_the_name_that_differs_from_the_schedule_is_flagged(env):
    sid = env.db.add_schedule(name="NOC Daily Briefing 08:00 Riyadh", frequency="daily", hour=5,
                              timezone="Asia/Riyadh")
    row = env.client.get(f"/api/v1/report-schedules/{sid}").json()
    assert row["name_time_mismatch"] == "08:00"
    assert row["local_time_label"] == "05:00 Asia/Riyadh (02:00 UTC)"
    fixed = _put(env, sid, {"hour": 8}).json()           # the one-by-one correction
    assert fixed["name_time_mismatch"] is None
    assert datetime.fromisoformat(fixed["next_run_at"]).hour == 5
    assert env.db.audits[-1]["metadata"]["changed"] == ["hour"]


def test_audit_records_the_timing(env):
    sid = _post(env, {"name": "Weekly", "report_type": "availability", "frequency": "weekly", "day_of_week": 7,
                      "hour": 8, "minute": 30, "timezone": "Europe/Berlin"}).json()["id"]
    _put(env, sid, {"hour": 7})
    create, update = env.db.audits[-2:]
    for a, hour in ((create, 8), (update, 7)):
        meta = a["metadata"]
        assert (meta["frequency"], meta["hour"], meta["minute"], meta["timezone"], meta["day_of_week"]) == \
            ("weekly", hour, 30, "Europe/Berlin", 7)
