"""Delivered reports and delivery settings (docs/reports-delivery.md): the
paged run history with link and address visibility, the stored attachment,
revoking a link, retrying deliveries, the report settings, and the legacy
email wording.

Runs against an in-memory double; the app's startup hooks are not run (the
client is not used as a context manager), so nothing touches a database.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import report_runs as RR
from app.api.v1 import report_schedules as RS
from app.core import scoping, security
from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app
from app.services import report_html, report_settings

ROLES = {
    "rdx_admin": ["system.admin"],
    "rdx_manager": ["reports.view", "reports.manage", "reports.export"],
    "rdx_viewer": ["reports.view", "reports.export"],
}


def _user(role: str):
    return SimpleNamespace(id=uuid.uuid4(), username=role, full_name=role, role=role, is_active=True,
                           scope_tags=[])


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


class RunsDB:
    def __init__(self):
        self.runs: dict[str, dict] = {}
        self.settings: dict | None = None
        self.audits: list[dict] = []

    def add_run(self, **kw) -> str:
        rid = str(uuid.uuid4())
        row = dict(id=uuid.UUID(rid), schedule_id=uuid.uuid4(), title="Weekly SLA", report_type="availability",
                   period="last_7d", token="tok-" + rid[:8], status="success", delivered_to=["ops@example.com"],
                   error=None, generated_at=datetime.now(timezone.utc),
                   expires_at=datetime.now(timezone.utc) + timedelta(days=30), kind="scheduled", notes=[],
                   failed_targets=[], retry_count=0, next_retry_at=None, artifact_name="weekly.pdf",
                   artifact=b"%PDF", artifact_mime="application/pdf", schedule_name="Weekly SLA", owner_id=None)
        row.update(kw)
        row["has_artifact"] = row["artifact"] is not None
        self.runs[rid] = row
        return rid

    async def commit(self):
        return None

    async def rollback(self):
        return None

    def begin_nested(self):
        class _N:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False
        return _N()

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        params = dict(params or {})
        if sql.startswith("INSERT INTO audit_logs"):
            self.audits.append({**params, "metadata": json.loads(params["metadata"])})
            return _Res()
        if "FROM system_settings WHERE key = :k" in sql:
            return _Res([(self.settings,)] if self.settings is not None else [])
        if sql.startswith("INSERT INTO system_settings"):
            self.settings = json.loads(params["v"])
            return _Res()
        rows = list(self.runs.values())
        if "r.schedule_id = :sid" in sql:
            rows = [r for r in rows if r["schedule_id"] == params["sid"]]
        if "r.status = :st" in sql:
            rows = [r for r in rows if r["status"] == params["st"]]
        if "s.created_by = CAST(:me AS uuid)" in sql:
            rows = [r for r in rows if str(r["owner_id"]) == params["me"]]
        if sql.startswith("SELECT count(*)"):
            return _Res(scalar=len(rows))
        if "WHERE r.id = :id" in sql:
            row = self.runs.get(str(params["id"]))
            return _Res([SimpleNamespace(**row)] if row else [])
        if sql.startswith("SELECT artifact"):
            row = self.runs.get(str(params["id"]))
            return _Res([SimpleNamespace(**row)] if row else [])
        if sql.startswith("SELECT r.id"):
            rows.sort(key=lambda r: r["generated_at"], reverse=True)
            rows = rows[params["off"]:params["off"] + params["lim"]]
            return _Res([SimpleNamespace(**r) for r in rows])
        if sql.startswith("UPDATE report_runs SET expires_at = NOW()"):
            self.runs[str(params["id"])]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
            return _Res()
        if sql.startswith("UPDATE report_runs SET next_retry_at = NULL"):
            row = self.runs[str(params["id"])]
            return _Res([SimpleNamespace(id=row["id"])] if row["failed_targets"] else [])
        raise AssertionError(f"unexpected SQL: {sql[:120]}")


@pytest.fixture
def env(monkeypatch):
    db = RunsDB()
    state = {"user": _user("rdx_admin"), "scoped": False}

    async def role_perms(_db, role):
        return list(ROLES.get(role, []))

    async def visible(_db, user):
        return ["team-a"] if state["scoped"] else None

    monkeypatch.setattr(security, "get_role_permissions", role_perms)
    monkeypatch.setattr(RS, "get_role_permissions", role_perms)
    monkeypatch.setattr(RR, "get_role_permissions", role_perms)
    monkeypatch.setattr(scoping, "get_role_permissions", role_perms)
    monkeypatch.setattr(scoping, "visible_tags", visible)

    async def fake_db():
        yield db

    async def current_user():
        return state["user"]

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = current_user
    client = TestClient(app)
    yield SimpleNamespace(db=db, state=state, client=client)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def test_runs_are_paged_and_filtered(env):
    for i in range(5):
        env.db.add_run(generated_at=datetime(2026, 10, 1 + i, tzinfo=timezone.utc),
                       status="failed" if i == 4 else "success")
    body = env.client.get("/api/v1/reports/runs?limit=2&offset=1").json()
    assert body["total"] == 5 and len(body["data"]) == 2 and (body["limit"], body["offset"]) == (2, 1)
    assert body["data"][0]["generated_at"].startswith("2026-10-04")
    failed = env.client.get("/api/v1/reports/runs?status=failed").json()
    assert failed["total"] == 1 and failed["data"][0]["status"] == "failed"
    assert env.client.get("/api/v1/reports/runs?limit=501").status_code == 422
    assert env.client.get("/api/v1/reports/runs?status=weird").status_code == 422


def test_links_and_addresses_only_for_managers_and_owners(env):
    owner = _user("rdx_viewer")
    mine = env.db.add_run(owner_id=owner.id)
    theirs = env.db.add_run(owner_id=uuid.uuid4())
    env.state["user"] = owner
    rows = {r["id"]: r for r in env.client.get("/api/v1/reports/runs").json()["data"]}
    assert rows[mine]["token"] and rows[mine]["delivered_to"] == ["ops@example.com"]
    assert rows[mine]["has_artifact"] is True
    other = rows[theirs]
    assert other["token"] is None and other["token_withheld"] is True
    assert other["delivered_to"] == [] and other["delivered_count"] == 1 and other["has_artifact"] is False
    assert env.client.get(f"/api/v1/reports/runs/{theirs}/artifact").status_code == 404
    got = env.client.get(f"/api/v1/reports/runs/{mine}/artifact")
    assert got.status_code == 200 and got.content == b"%PDF"
    assert 'filename="weekly.pdf"' in got.headers["content-disposition"]


def test_a_scoped_account_sees_only_its_own_runs(env):
    me = _user("rdx_viewer")
    mine = env.db.add_run(owner_id=me.id)
    env.db.add_run(owner_id=uuid.uuid4())
    env.state.update(user=me, scoped=True)
    body = env.client.get("/api/v1/reports/runs").json()
    assert [r["id"] for r in body["data"]] == [mine] and body["total"] == 1


def test_downloading_needs_reports_export(env, monkeypatch):
    rid = env.db.add_run()
    ROLES["rdx_noexport"] = ["reports.view", "reports.manage"]
    env.state["user"] = _user("rdx_noexport")
    assert env.client.get(f"/api/v1/reports/runs/{rid}/artifact").status_code == 403
    env.state["user"] = _user("rdx_admin")
    gone = env.db.add_run(artifact=None)
    assert env.client.get(f"/api/v1/reports/runs/{gone}/artifact").status_code == 404


def test_revoke_expires_the_link(env):
    rid = env.db.add_run()
    body = env.client.post(f"/api/v1/reports/runs/{rid}/revoke").json()
    assert body["revoked"] is True and body["token"] is None
    assert env.db.audits[-1]["action"] == "reports.run.revoke_link"
    env.state["user"] = _user("rdx_viewer")          # neither owner nor manager
    assert env.client.post(f"/api/v1/reports/runs/{rid}/revoke").status_code == 404


def test_retry_needs_failed_targets_and_reports_the_result(env, monkeypatch):
    clean = env.db.add_run()
    assert env.client.post(f"/api/v1/reports/runs/{clean}/retry").status_code == 409
    owed = env.db.add_run(status="failed",
                          failed_targets=[{"kind": "email", "id": "a@example.com", "name": "a@example.com",
                                           "error": "451"}])

    async def fake_retry(db, run_id, manual=False):
        assert manual is True and run_id == owed
        db.runs[run_id].update(failed_targets=[], status="success")
        return {"status": "success", "delivered": ["a@example.com"], "errors": [], "remaining": 0}

    from app.services import report_scheduler
    monkeypatch.setattr(report_scheduler, "retry_run", fake_retry)
    body = env.client.post(f"/api/v1/reports/runs/{owed}/retry").json()
    assert body["result"]["remaining"] == 0 and body["run"]["status"] == "success"
    assert env.db.audits[-1]["action"] == "reports.run.retry"
    env.state["user"] = _user("rdx_viewer")
    assert env.client.post(f"/api/v1/reports/runs/{owed}/retry").status_code == 403


def test_report_settings_defaults_validation_and_audit(env):
    got = env.client.get("/api/v1/reports/settings").json()
    assert got["settings"] == report_settings.DEFAULTS
    assert got["limits"]["link_expiry_days"] == {"default": 30, "min": 0, "max": 365}
    for bad in ({"link_expiry_days": 400}, {"retry_attempts": "3"}, {"nope": 1}, {"max_attachment_mb": True}):
        assert env.client.put("/api/v1/reports/settings", json=bad).status_code == 400, bad
    saved = env.client.put("/api/v1/reports/settings", json={"link_expiry_days": 0, "retry_attempts": 5}).json()
    assert saved["settings"]["link_expiry_days"] == 0 and saved["settings"]["retry_attempts"] == 5
    assert saved["settings"]["run_retention_days"] == 180
    assert env.db.audits[-1]["metadata"] == {"link_expiry_days": {"from": 30, "to": 0},
                                             "retry_attempts": {"from": 3, "to": 5}}
    env.state["user"] = _user("rdx_viewer")
    assert env.client.put("/api/v1/reports/settings", json={"retry_attempts": 1}).status_code == 403


def test_settings_normalise_stored_values():
    assert report_settings.normalize({"link_expiry_days": 9999, "retry_attempts": "x", "extra": 1}) == \
        {**report_settings.DEFAULTS, "link_expiry_days": 365}
    assert [report_settings.retry_delay_minutes(n) for n in (1, 2, 3, 9)] == [5, 15, 45, 240]


def test_legacy_email_names_the_schedule_and_the_manual_run():
    meta = {"title": "Exec", "report_type": "executive_summary", "schedule_name": "Weekly exec",
            "run_kind": "manual", "run_note": "Manual run: generated on request.",
            "delivery_note": "This email has no link to the full report."}
    html = report_html.build_report_email_html(meta, {"kpis": {}}, None, attached=False)
    assert "MANUAL RUN" in html and "SCHEDULED REPORT" not in html
    assert "notification channel" not in html and "Weekly exec" in html
    assert "Manual run: generated on request." in html and "This email has no link" in html
    text = report_html.build_report_email_text(meta, {"kpis": {}}, None)
    assert "Manual run: generated on request." in text and "This email has no link" in text
    scheduled = report_html.build_report_email_html({**meta, "run_kind": "scheduled", "run_note": None},
                                                    {"kpis": {}}, None)
    assert "SCHEDULED REPORT" in scheduled and "manual run" not in scheduled.lower().replace("manual run:", "")


def test_runs_routes_need_reports_view_and_writes_manage():
    routes = {(r.path, m): r for r in RR.router.routes for m in r.methods}
    for path, method, perm in (("/reports/runs", "GET", "reports.view"),
                               ("/reports/runs/{run_id}/retry", "POST", "reports.manage"),
                               ("/reports/settings", "PUT", "reports.manage")):
        names = [getattr(d.call, "__qualname__", "") for d in routes[(path, method)].dependant.dependencies]
        assert any("require_permission" in n for n in names), (path, names)


def test_a_busy_retry_is_409_and_notes_hide_addresses_from_others(env, monkeypatch):
    owed = env.db.add_run(status="failed", notes=["Retry 1: delivered to a@example.com"],
                          failed_targets=[{"kind": "email", "id": "b@example.com", "name": "b@example.com",
                                           "error": "451"}])

    async def busy(db, run_id, manual=False):
        return {"status": "busy", "delivered": [], "errors": ["A retry of this run is already in progress"]}

    from app.services import report_scheduler
    monkeypatch.setattr(report_scheduler, "retry_run", busy)
    r = env.client.post(f"/api/v1/reports/runs/{owed}/retry")
    assert r.status_code == 409 and "already in progress" in r.json()["detail"]
    env.state["user"] = _user("rdx_viewer")
    row = next(x for x in env.client.get("/api/v1/reports/runs").json()["data"] if x["id"] == owed)
    assert row["notes"] == ["Retry 1: delivered to [address]"] and row["failed_targets"] == []
