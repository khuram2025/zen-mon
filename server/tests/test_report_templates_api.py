"""Organisation templates: authors may only add sections they can see, and a
forced delete removes dependent schedules only for someone allowed to."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import report_schedules as RS
from app.core import scoping, security
from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app

ROLES = {
    "tpl_admin": ["system.admin"],
    "tpl_manager": ["reports.view", "reports.manage", "devices.view"],
    "tpl_mgr_full": ["reports.view", "reports.manage", "devices.view", "audit.view"],
}


def _user(role):
    return SimpleNamespace(id=uuid.uuid4(), username=role, full_name=role, role=role,
                           is_active=True, scope_tags=[])


class _Null:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Res:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.rowcount = len(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class TemplateDB:
    def __init__(self):
        self.templates: dict[str, dict] = {}
        self.schedules: dict[str, SimpleNamespace] = {}
        self.audits: list[dict] = []

    def begin_nested(self):
        return _Null()

    async def commit(self):
        return None

    async def rollback(self):
        return None

    def _row(self, t):
        return SimpleNamespace(**t, schedules=sum(1 for s in self.schedules.values()
                                                  if s.custom_report_id == t["id"]))

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        p = dict(params or {})
        if sql.startswith("INSERT INTO audit_logs"):
            self.audits.append({**p, "metadata": json.loads(p["metadata"])})
            return _Res()
        if sql.startswith("INSERT INTO custom_reports"):
            tid = str(uuid.uuid4())
            self.templates[tid] = dict(
                id=tid, name=p["n"], description=p["d"], sections=json.loads(p["s"]),
                definition=json.loads(p["def"]), category=p["cat"], tags=json.loads(p["tags"]),
                visibility=p.get("vis", "private"), source_key=p["src"], default_period=p["per"],
                owner_id=p["u"], owner_name=None, created_at=None, updated_at=None)
            return _Res([(tid,)])
        if "FROM custom_reports c" in sql and "WHERE c.id" in sql:
            t = self.templates.get(str(p["id"]))
            return _Res([self._row(t)] if t else [])
        if sql.startswith("UPDATE custom_reports"):
            t = self.templates[str(p["id"])]
            t.update(name=p["n"], sections=json.loads(p["s"]), definition=json.loads(p["def"]),
                     visibility=p["vis"])
            return _Res([t])
        if sql.startswith("SELECT id, name, report_type, period, format, created_by FROM report_schedules"):
            return _Res([s for s in self.schedules.values() if s.custom_report_id == str(p["id"])])
        if sql.startswith("DELETE FROM report_schedules"):
            self.schedules.pop(str(p["id"]), None)
            return _Res([1])
        if sql.startswith("DELETE FROM custom_reports"):
            self.templates.pop(str(p["id"]), None)
            return _Res([1])
        raise AssertionError(f"unexpected SQL: {sql[:120]}")


@pytest.fixture
def env(monkeypatch):
    db = TemplateDB()
    state = {"user": _user("tpl_admin")}

    async def role_perms(_db, role):
        return list(ROLES.get(role, []))

    async def no_role_scope(_db, role):
        return []

    monkeypatch.setattr(security, "get_role_permissions", role_perms)
    monkeypatch.setattr(RS, "get_role_permissions", role_perms)
    monkeypatch.setattr(scoping, "get_role_permissions", role_perms)
    monkeypatch.setattr(scoping, "get_role_scope_tags", no_role_scope)

    async def fake_db():
        yield db

    async def current_user():
        return state["user"]

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = current_user
    yield SimpleNamespace(db=db, state=state, client=TestClient(app))
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def _body(sections, **kw):
    return {"name": "Ops pack", "sections": sections, "visibility": "shared", **kw}


def test_authors_cannot_add_sections_they_may_not_see(env):
    env.state["user"] = _user("tpl_manager")          # devices.view, no audit.view
    r = env.client.post("/api/v1/reports/custom", json=_body(["device_status_now", "audit_activity"]))
    assert r.status_code == 403 and "Administrative Audit Trail" in r.json()["detail"]
    r = env.client.post("/api/v1/reports/custom", json=_body(["device_status_now"]))
    assert r.status_code == 201, r.text


def test_editing_keeps_existing_sections_but_refuses_new_unviewable_ones(env):
    env.state["user"] = _user("tpl_mgr_full")
    tid = env.client.post("/api/v1/reports/custom",
                          json=_body(["device_status_now", "audit_activity"])).json()["id"]
    env.state["user"] = _user("tpl_manager")          # may edit shared templates, cannot see audit
    keep = env.client.put(f"/api/v1/reports/custom/{tid}",
                          json=_body(["audit_activity", "device_status_now"], name="Reordered"))
    assert keep.status_code == 200, keep.text
    add = env.client.put(f"/api/v1/reports/custom/{tid}",
                         json=_body(["audit_activity", "device_status_now", "login_activity"]))
    assert add.status_code == 403


def test_forced_delete_removes_only_schedules_the_caller_may_delete(env):
    owner = _user("tpl_manager")
    env.state["user"] = owner
    tid = env.client.post("/api/v1/reports/custom", json=_body(["device_status_now"])).json()["id"]
    admin = _user("tpl_admin")
    env.db.schedules["s1"] = SimpleNamespace(id="s1", name="Admin's weekly", report_type="custom",
                                             period="last_7d", format="pdf", custom_report_id=tid,
                                             created_by=admin.id)
    # Plain delete: 409 because a schedule delivers it.
    assert env.client.delete(f"/api/v1/reports/custom/{tid}").status_code == 409

    # The template owner is not a report manager here (the fixture's manager
    # role is not limited to a scope, so make them a non-manager by role):
    ROLES["tpl_owner_only"] = ["reports.view", "devices.view"]
    try:
        env.db.templates[tid]["owner_id"] = str(owner.id)
        owner.role = "tpl_owner_only"
        # reports.manage is required for the endpoint at all.
        assert env.client.delete(f"/api/v1/reports/custom/{tid}?force=true").status_code == 403
    finally:
        ROLES.pop("tpl_owner_only")
        owner.role = "tpl_manager"

    # A report manager may force it; the schedule delete is audited.
    r = env.client.delete(f"/api/v1/reports/custom/{tid}?force=true")
    assert r.status_code == 200, r.text
    assert "s1" not in env.db.schedules and tid not in env.db.templates
    actions = [a["action"] for a in env.db.audits]
    assert "reports.schedule.delete" in actions and actions[-1] == "reports.template.delete"
    sched_audit = next(a for a in env.db.audits if a["action"] == "reports.schedule.delete")
    assert sched_audit["metadata"]["reason"] == "template deleted"
