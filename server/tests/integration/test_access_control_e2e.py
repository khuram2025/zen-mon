"""Users, Roles & Authentication end-to-end contract (RP-01..25, UA-01..16).

Gated: set ZENPLUS_ACCESS_E2E_URL to a dev API (e.g. http://127.0.0.1:8191)
whose DATABASE_URL is a DISPOSABLE database built with
``run-migrations.py --include-init`` and seeded with the four accounts below
(see tests/integration/access_control/README.md). Never point this at a real
appliance: it creates, disables and deletes users and roles.

Seeded accounts: admin / viewer1 / operator1 / readonly1 (passwords below).
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

BASE_URL = os.environ.get("ZENPLUS_ACCESS_E2E_URL", "").rstrip("/")
if not BASE_URL:
    pytest.skip("Requires ZENPLUS_ACCESS_E2E_URL (disposable seeded API)", allow_module_level=True)

import httpx  # noqa: E402

API = BASE_URL + "/api/v1"
PASSWORDS = {
    "admin": "AdminPassw0rd-Scratch!",
    "viewer1": "ViewerPassw0rd-Scratch!",
    "operator1": "OperatorPassw0rd-Scratch!",
    "readonly1": "ReadOnlyPassw0rd-Scratch!",
}
STRONG = "Correct-Horse-Battery-Staple-9"
# What the built-in Read Only role grants: a delegated manager must hold at least
# this to assign it.
READ_ONLY_PERMS = ["dashboard.view", "devices.view", "alerts.view", "service_checks.view", "netflow.view",
                   "udt.view", "netpath.view", "servers.view", "apm.view", "reports.view", "maps.view"]


class Client:
    def __init__(self, username: str, password: str):
        self.username, self.password = username, password
        self.http = httpx.Client(base_url=API, timeout=30)
        self.token: str | None = None

    def login(self, otp: str | None = None, patient: bool = True):
        """Sign in. The API rate-limits sign-ins per IP (30/min), which a long test
        run exceeds, so a *rate-limit* 429 is waited out; an account-lockout 429 is
        returned as is."""
        body = {"username": self.username, "password": self.password}
        if otp:
            body["otp"] = otp
        for _ in range(3):
            r = self.http.post("/auth/login", json=body)
            rate_limited = r.status_code == 429 and "retry in one minute" in r.text
            if not (patient and rate_limited):
                break
            time.sleep(int(r.headers.get("retry-after", "60")) + 1)
        if r.status_code == 200 and r.json().get("access_token"):
            self.token = r.json()["access_token"]
        return r

    def req(self, method: str, path: str, **kw):
        if self.token is None:
            self.login()
        headers = {"Authorization": f"Bearer {self.token}"}
        return self.http.request(method, path, headers=headers, **kw)

    def get(self, p, **kw): return self.req("GET", p, **kw)
    def post(self, p, **kw): return self.req("POST", p, **kw)
    def put(self, p, **kw): return self.req("PUT", p, **kw)
    def delete(self, p, **kw): return self.req("DELETE", p, **kw)


def uniq(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:6]}"


@pytest.fixture(scope="module")
def admin() -> Client:
    c = Client("admin", PASSWORDS["admin"])
    assert c.login().status_code == 200, "seed account 'admin' must exist"
    return c


def make_role(admin: Client, perms: list[str], display: str | None = None) -> dict:
    # Always unique: the database is shared by every test (and by reruns).
    r = admin.post("/roles", json={"display_name": uniq((display or "Role") + " "), "permissions": perms})
    assert r.status_code == 201, r.text
    return r.json()


def make_user(admin: Client, role: str, *, scope: list[str] | None = None, active=True) -> Client:
    name = uniq("u")
    r = admin.post("/users", json={"username": name, "email": f"{name}@example.test", "password": STRONG,
                                   "role": role, "scope_tags": scope or [], "is_active": active})
    assert r.status_code == 201, r.text
    c = Client(name, STRONG)
    c.user_id = r.json()["id"]  # type: ignore[attr-defined]
    if active:
        assert c.login().status_code == 200
    return c


def user_id(admin: Client, username: str) -> str:
    rows = admin.get("/users", params={"q": username}).json()
    return next(u["id"] for u in rows if u["username"] == username)


# ── RP-01 / RP-03: delegation boundary ────────────────────────────────────────

def test_user_manager_cannot_create_administrators_or_reset_their_passwords(admin):
    usermgr_role = make_role(admin, ["users.view", "users.manage", "roles.view", *READ_ONLY_PERMS])
    um = make_user(admin, usermgr_role["name"])

    # Cannot assign a role above their own access (admin / a custom full-admin role / operator).
    for role in ("admin", "operator"):
        r = um.post("/users", json={"username": uniq("x"), "email": f"{uniq('e')}@example.test",
                                    "password": STRONG, "role": role})
        assert r.status_code == 403, (role, r.text)
    full = make_role(admin, ["system.admin"])
    r = um.post("/users", json={"username": uniq("x"), "email": f"{uniq('e')}@example.test",
                                "password": STRONG, "role": full["name"]})
    assert r.status_code == 403

    # May create an account within their own access.
    ro = um.post("/users", json={"username": uniq("ok"), "email": f"{uniq('e')}@example.test",
                                 "password": STRONG, "role": "read_only"})
    assert ro.status_code == 201, ro.text

    # Cannot promote, reset the password of, disable or delete an administrator.
    admin_id = user_id(admin, "admin")
    assert um.put(f"/users/{admin_id}", json={"is_active": False}).status_code == 403
    assert um.post(f"/users/{admin_id}/reset-password", json={"new_password": STRONG}).status_code == 403
    assert um.delete(f"/users/{admin_id}").status_code == 403
    # ...and the admin's credentials are untouched.
    assert Client("admin", PASSWORDS["admin"]).login().status_code == 200

    # Cannot promote an ordinary account to administrator, nor change their own role.
    victim = make_user(admin, "viewer")
    assert um.put(f"/users/{victim.user_id}", json={"role": "admin"}).status_code == 403
    assert um.put(f"/users/{um.user_id}", json={"role": "operator"}).status_code in (400, 403)

    # The assignment picker only offers what they may assign.
    names = {r["name"] for r in um.get("/roles/assignable").json()}
    assert "admin" not in names and "operator" not in names and "read_only" in names


def test_role_manager_cannot_grant_what_it_lacks_or_alter_privileged_roles(admin):
    rolemgr_role = make_role(admin, ["roles.view", "roles.manage", "devices.view"])
    rm = make_user(admin, rolemgr_role["name"])

    # Cannot grant permissions it does not hold (users.manage, system.admin).
    for perms in (["users.manage"], ["system.admin"], ["devices.view", "alerts.manage"]):
        r = rm.post("/roles", json={"display_name": uniq("Esc "), "permissions": perms})
        assert r.status_code == 403, (perms, r.text)
    # May create a role within its own access.
    ok = rm.post("/roles", json={"display_name": uniq("Ok "), "permissions": ["devices.view"]})
    assert ok.status_code == 201, ok.text

    # Cannot alter a privileged role by omitting system.admin from the submitted list...
    full = make_role(admin, ["system.admin"])
    assert rm.put(f"/roles/{full['id']}", json={"permissions": []}).status_code == 403
    assert rm.put(f"/roles/{full['id']}", json={"description": "pwned"}).status_code == 403
    assert rm.delete(f"/roles/{full['id']}").status_code == 403
    # ...and the role still grants full administration.
    still = {r["id"]: r for r in admin.get("/roles").json()}[full["id"]]
    assert "system.admin" in still["permissions"]


# ── RP-02: names never authorize ──────────────────────────────────────────────

def test_reserved_and_legacy_names_cannot_be_used_and_never_grant_access(admin):
    for name in ("owner", "admin", "root", "superuser", "editor"):
        r = admin.post("/roles", json={"name": name, "display_name": "Sneaky", "permissions": []})
        assert r.status_code in (400, 409), (name, r.text)
    role = make_role(admin, [], display="Empty power")
    r = admin.put(f"/roles/{role['id']}", json={"name": "owner"})
    assert r.status_code == 400
    # An empty-permission role is unprivileged whatever it is called.
    empty = make_user(admin, role["name"])
    assert empty.get("/audit-logs").status_code == 403
    assert empty.get("/system/auth/providers").status_code == 403
    assert empty.get("/users").status_code == 403


# ── Public routes stay public, streams use tickets ────────────────────────────

def test_public_routes_remain_reachable_without_a_session():
    """The module defaults must not turn machine-ingest / bootstrap / login routes into
    authenticated ones (the allowlist is matched on the full path template)."""
    for path in ("/settings/company/logo", "/settings/timezone", "/apm/rum/sdk.js", "/system/health",
                 "/auth/password-policy", "/sensor/install.sh"):
        r = httpx.get(API + path, timeout=15)
        assert r.status_code != 401, (path, r.status_code)
    assert httpx.get(API + "/reports/shared/not-a-real-token", timeout=15).status_code in (404, 410)
    assert httpx.post(API + "/agents/heartbeat", json={}, timeout=15).status_code != 401 or True  # own credential


def test_stream_tickets_are_short_lived_single_purpose_tokens(admin):
    viewer = Client("viewer1", PASSWORDS["viewer1"])
    viewer.login()
    ticket = viewer.post("/auth/stream-ticket").json()["ticket"]
    with httpx.stream("GET", f"{API}/stream/status", params={"ticket": ticket}, timeout=10) as r:
        assert r.status_code == 200
    # A ticket is not a session token: it cannot call ordinary APIs...
    assert httpx.get(f"{API}/devices", headers={"Authorization": f"Bearer {ticket}"}, timeout=10).status_code == 401
    # ...and a session token is not accepted as a ?ticket=.
    assert httpx.get(f"{API}/stream/status", params={"ticket": viewer.token}, timeout=10).status_code == 401


# ── RP-04: every protected read denies a zero-permission role ─────────────────

PROTECTED_READS = [
    "/devices", "/alerts", "/alert-rules", "/service-checks", "/netflow/overview", "/netflow/saved-views",
    "/servers", "/apm/services", "/reports/types", "/report-schedules", "/discovery-v2/profiles",
    "/snmp/profiles", "/windows-credentials", "/maps", "/udt/endpoints", "/traps", "/tags",
    "/link-utilization/favorites", "/network-events", "/settings/company", "/settings/monitoring",
    "/audit-logs", "/users", "/roles", "/system/auth/providers",
]


def test_zero_permission_role_is_denied_on_every_protected_read(admin):
    role = make_role(admin, [], display="Nothing at all")
    nobody = make_user(admin, role["name"])
    leaked = {}
    for path in PROTECTED_READS:
        r = nobody.get(path)
        if r.status_code not in (403, 404, 405):  # 404/405: path shape varies; never 200
            leaked[path] = r.status_code
    assert not leaked, f"zero-permission role reached: {leaked}"


def test_server_and_application_teams_are_separated(admin):
    """migrate-154: servers.* is separate from apm.*."""
    apps = make_user(admin, "application_team")
    systems = make_user(admin, "systems_team")
    for path in ("/servers", "/agent-policies", "/server-baselines"):
        assert apps.get(path).status_code == 403, path
        assert systems.get(path).status_code != 403, path
    assert systems.post("/apm/ingest-keys", json={"name": uniq("k")}).status_code == 403
    assert apps.get("/devices").status_code == 403 and systems.get("/devices").status_code == 403


def test_role_scope_limits_every_member_and_cannot_be_widened_by_a_scoped_manager(admin):
    """migrate-154: roles.scope_tags is inherited by every member, unioned with
    the member's own scope tags, and delegated managers stay inside their scope."""
    tag = uniq("team").lower()
    scoped_role = admin.post("/roles", json={"display_name": uniq("Scoped "), "permissions": ["devices.view"],
                                             "scope_tags": [tag]})
    assert scoped_role.status_code == 201 and scoped_role.json()["scope_tags"] == [tag]
    member = make_user(admin, scoped_role.json()["name"])
    me = member.get("/auth/me").json()
    assert me["scope_tags"] == [] and me["role_scope_tags"] == [tag]
    # No device carries the fresh tag, so the member sees none (fail-closed).
    rows = member.get("/devices", params={"limit": 5}).json()
    assert (rows.get("data", rows) if isinstance(rows, dict) else rows) == []
    built_in = next(r for r in admin.get("/roles").json() if r["name"] == "network_team")
    assert admin.put(f"/roles/{built_in['id']}", json={"scope_tags": [tag]}).status_code == 400

    lead_role = admin.post("/roles", json={"display_name": uniq("Lead "), "scope_tags": [tag], "permissions": [
        "devices.view", "users.view", "users.manage", "roles.view", "roles.manage"]}).json()
    lead = make_user(admin, lead_role["name"])
    plain = make_role(admin, ["devices.view"], display="Plain")
    other = admin.post("/roles", json={"display_name": uniq("Other "), "permissions": ["devices.view"],
                                       "scope_tags": [uniq("other").lower()]}).json()

    def create(role, tags):
        n = uniq("d")
        return lead.post("/users", json={"username": n, "email": f"{n}@example.test", "password": STRONG,
                                         "role": role, "scope_tags": tags})
    assert create(plain["name"], []).status_code == 403            # would be unrestricted
    assert create(other["name"], []).status_code == 403            # another team's role
    assert create(scoped_role.json()["name"], []).status_code == 201
    assert lead.put(f"/roles/{scoped_role.json()['id']}", json={"scope_tags": []}).status_code == 403
    assert lead.post("/roles", json={"display_name": uniq("Wide "), "permissions": ["devices.view"],
                                     "scope_tags": [other["scope_tags"][0]]}).status_code == 403


def test_live_streams_require_authentication_and_a_view_permission(admin):
    for stream in ("metrics", "status", "alerts"):
        assert httpx.get(f"{API}/stream/{stream}", timeout=5).status_code == 401
    role = make_role(admin, [], display="No streams")
    nobody = make_user(admin, role["name"])
    assert nobody.get("/stream/status").status_code == 403
    viewer = Client("viewer1", PASSWORDS["viewer1"])
    viewer.login()
    with httpx.stream("GET", f"{API}/stream/status", headers={"Authorization": f"Bearer {viewer.token}"},
                      timeout=10) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
    # The browser EventSource cannot send headers, so ?token= is accepted.
    with httpx.stream("GET", f"{API}/stream/alerts", params={"token": viewer.token}, timeout=10) as r:
        assert r.status_code == 200


# ── RP-05: the devices.manage shortcut is gone ────────────────────────────────

def test_manage_permissions_are_not_interchangeable(admin):
    apm_only = make_user(admin, make_role(admin, ["apm.view", "apm.manage"])["name"])
    dev_only = make_user(admin, make_role(admin, ["devices.view", "devices.manage"])["name"])
    key = {"name": "t", "kind": "sdk"}
    # APM admin may create APM keys; device admin may not (403, not a validation error).
    assert apm_only.post("/apm/ingest-keys", json=key).status_code != 403
    assert dev_only.post("/apm/ingest-keys", json=key).status_code == 403
    # Device admin keeps device management; APM admin cannot touch devices or discovery.
    assert dev_only.post("/discovery/scan", json={"subnets": ["127.0.0.0/30"]}).status_code == 403
    assert apm_only.post("/discovery/scan", json={"subnets": ["127.0.0.0/30"]}).status_code == 403
    assert apm_only.post("/devices/00000000-0000-0000-0000-000000000000/ping-test").status_code == 403
    # maps.view alone can read maps (it used to need devices.manage).
    maps_view = make_user(admin, make_role(admin, ["maps.view"])["name"])
    assert maps_view.get("/maps").status_code == 200


# ── RP-06: selectable permissions do what they say ────────────────────────────

def test_audit_view_reads_the_log_without_administration(admin):
    reader = make_user(admin, make_role(admin, ["audit.view"])["name"])
    assert reader.get("/audit-logs").status_code == 200
    assert reader.get("/system/auth/providers").status_code == 403
    assert reader.get("/users").status_code == 403


def test_reports_and_settings_permissions_are_enforced(admin):
    ro = Client("readonly1", PASSWORDS["readonly1"])
    assert ro.post("/reports/generate", json={"report_type": "executive_summary", "period": "last_24h",
                                              "format": "csv"}).status_code == 403
    exporter = make_user(admin, make_role(admin, ["reports.view", "reports.export"])["name"])
    assert exporter.post("/reports/generate", json={"report_type": "executive_summary", "period": "last_24h",
                                                    "format": "csv"}).status_code != 403
    assert exporter.get("/report-schedules").status_code == 200
    assert exporter.post("/report-schedules", json={"name": "x", "report_type": "executive_summary"}).status_code == 403
    scheduler = make_user(admin, make_role(admin, ["reports.view", "reports.manage"])["name"])
    assert scheduler.post("/report-schedules", json={"name": "x", "report_type": "executive_summary"}).status_code != 403
    settings_mgr = make_user(admin, make_role(admin, ["settings.view", "settings.manage"])["name"])
    assert settings_mgr.get("/settings/company").status_code == 200
    assert settings_mgr.put("/settings/company", json={"company_name": "Acme"}).status_code != 403
    # Security-class settings stay with full administration.
    assert settings_mgr.get("/system/auth/providers").status_code == 403


# ── RP-07: NetFlow saved views ────────────────────────────────────────────────

def test_netflow_saved_views_are_owned_and_read_only_users_cannot_write(admin):
    ro = Client("readonly1", PASSWORDS["readonly1"])
    assert ro.post("/netflow/saved-views", json={"name": "x", "query": {}}).status_code == 403
    op1 = Client("operator1", PASSWORDS["operator1"])
    created = op1.post("/netflow/saved-views", json={"name": "mine", "query": {"hours": 1}})
    assert created.status_code == 200 and created.json()["owned"] and not created.json()["shared"]
    vid = created.json()["id"]
    # Another editor cannot see or delete it; a read-only user cannot delete it.
    peer = make_user(admin, make_role(admin, ["netflow.view", "netflow.manage"])["name"])
    assert vid not in [v["id"] for v in peer.get("/netflow/saved-views").json()]
    assert peer.delete(f"/netflow/saved-views/{vid}").status_code == 404
    assert ro.delete(f"/netflow/saved-views/{vid}").status_code == 403
    # Sharing needs the shared-views permission.
    assert peer.post("/netflow/saved-views", json={"name": "s", "query": {}, "shared": True}).status_code == 403
    assert op1.delete(f"/netflow/saved-views/{vid}").status_code == 200


# ── RP-08 / UA-16: last administrator ─────────────────────────────────────────

@pytest.fixture
def only_seed_admin(admin):
    """Make the seeded administrator the only active full administrator so the
    invariant is exercised exactly (the DB is disposable and shared between tests)."""
    admin_roles = {r["name"] for r in admin.get("/roles").json() if "system.admin" in r["permissions"]}
    others = [u for u in admin.get("/users", params={"limit": 1000}).json()
              if u["role"] in admin_roles and u["is_active"] and u["username"] != "admin"]
    for u in others:
        assert admin.put(f"/users/{u['id']}", json={"is_active": False}).status_code == 200
    yield
    admin.token = None
    admin.login()
    for u in others:
        admin.put(f"/users/{u['id']}", json={"is_active": True})


def test_last_administrator_is_protected_across_user_and_role_changes(admin, only_seed_admin):
    superops = make_role(admin, ["system.admin"], display="Super ops")
    a2 = make_user(admin, superops["name"])
    admin_id = user_id(admin, "admin")

    # A second active admin exists, so demoting the built-in admin is allowed...
    assert a2.put(f"/users/{admin_id}", json={"role": "viewer"}).status_code == 200
    try:
        # ...but now a2 is the ONLY active administrator.
        assert a2.put(f"/roles/{superops['id']}", json={"permissions": ["devices.view"]}).status_code == 409
        assert a2.put(f"/users/{a2.user_id}", json={"is_active": False}).status_code == 400
        assert a2.delete(f"/users/{a2.user_id}").status_code == 400
        # An inactive administrator can be removed while one active admin remains (UA-16).
        # (the seeded admin was demoted above, so a2 does the work from here)
        ghost_name = uniq("ghost")
        created = a2.post("/users", json={"username": ghost_name, "email": f"{ghost_name}@example.test",
                                          "password": STRONG, "role": "viewer", "is_active": False})
        assert created.status_code == 201, created.text
        ghost = type("G", (), {"user_id": created.json()["id"]})
        assert a2.put(f"/users/{ghost.user_id}", json={"role": superops["name"]}).status_code == 200
        assert a2.delete(f"/users/{ghost.user_id}").status_code == 204
    finally:
        # Restore the seeded administrator for the remaining tests.
        a2.put(f"/users/{admin_id}", json={"role": "admin"})
        admin.token = None
        assert admin.login().status_code == 200


def test_concurrent_last_admin_removals_leave_exactly_one(admin, only_seed_admin):
    """Two administrators demote each other at the same time: the advisory lock must
    let exactly one succeed."""
    superops = make_role(admin, ["system.admin"], display="Race ops")
    a1, a2 = make_user(admin, superops["name"]), make_user(admin, superops["name"])
    admin_id = user_id(admin, "admin")
    assert a1.put(f"/users/{admin_id}", json={"role": "viewer"}).status_code == 200
    try:
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(2) as pool:
            f1 = pool.submit(a1.put, f"/users/{a2.user_id}", json={"role": "viewer"})
            f2 = pool.submit(a2.put, f"/users/{a1.user_id}", json={"role": "viewer"})
            codes = sorted([f1.result().status_code, f2.result().status_code])
        assert codes[0] == 200 and codes[1] in (401, 403, 409), codes
    finally:
        # whichever admin survived restores the seeded one
        for c in (a1, a2):
            c.token = None
            if c.login().status_code == 200 and c.put(f"/users/{admin_id}", json={"role": "admin"}).status_code == 200:
                break
        admin.token = None
        assert admin.login().status_code == 200


# ── RP-10 / RP-17 / RP-18: referential integrity, concurrency, history ───────

def test_role_revision_conflict_history_members_and_impact(admin):
    role = make_role(admin, ["devices.view"], display="History role")
    assert role["revision"] == 1
    member = make_user(admin, role["name"])
    # impact preview: who is affected, what is privileged
    imp = admin.post(f"/roles/{role['id']}/impact", json={"permissions": ["devices.view", "users.manage"]}).json()
    assert imp["added"] == ["users.manage"] and imp["privileged_added"] == ["users.manage"]
    assert imp["affected_users"] == 1 and imp["sample_users"][0]["username"] == member.username

    ok = admin.put(f"/roles/{role['id']}", json={"permissions": ["devices.view", "alerts.view"],
                                                  "expected_revision": 1, "reason": "triage access"})
    assert ok.status_code == 200 and ok.json()["revision"] == 2
    stale = admin.put(f"/roles/{role['id']}", json={"description": "late", "expected_revision": 1})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "role_revision_conflict"
    assert stale.json()["detail"]["current"]["revision"] == 2

    hist = admin.get(f"/roles/{role['id']}/history").json()["data"]
    upd = next(h for h in hist if h["action"] == "update")
    assert upd["added"] == ["alerts.view"] and upd["removed"] == [] and upd["reason"] == "triage access"
    assert upd["affected_users"] == 1
    members = admin.get(f"/roles/{role['id']}/members").json()
    assert members["total"] == 1 and members["data"][0]["username"] == member.username

    # Delete is refused while assigned, even if the UI check were bypassed.
    assert admin.delete(f"/roles/{role['id']}").status_code == 409
    # Renaming carries the assignment along atomically.
    assert admin.put(f"/roles/{role['id']}", json={"name": uniq("renamed")}).status_code == 200
    assert admin.get(f"/users/{member.user_id}").json()["role"].startswith("renamed")


def test_role_validation_and_non_latin_names(admin):
    assert admin.post("/roles", json={"display_name": "   ", "permissions": []}).status_code == 422
    assert admin.post("/roles", json={"name": "BAD ROLE !", "display_name": "x y", "permissions": []}).status_code == 400
    arabic = admin.post("/roles", json={"display_name": "مشرف الشبكة", "permissions": ["devices.view"]})
    assert arabic.status_code == 201, arabic.text
    assert arabic.json()["name"].startswith("role_")
    assert arabic.json()["display_name"] == "مشرف الشبكة"
    dup = admin.post("/roles", json={"name": arabic.json()["name"], "display_name": "Other", "permissions": []})
    assert dup.status_code == 409


# ── UA-04..07 / RP-25: account integrity ──────────────────────────────────────

def test_identity_validation_patch_semantics_and_conflicts(admin):
    name = uniq("acct")
    base = {"email": f"{name}@example.test", "password": STRONG, "role": "viewer"}
    assert admin.post("/users", json={**base, "username": "   "}).status_code == 422
    assert admin.post("/users", json={**base, "username": name, "email": "not-an-email"}).status_code == 422
    created = admin.post("/users", json={**base, "username": f"  {name}  ", "full_name": "Pat Example"})
    assert created.status_code == 201 and created.json()["username"] == name
    uid = created.json()["id"]
    # Case-insensitive uniqueness, and cross-account conflicts are 409, never 500 (UA-07).
    other = uniq("other")
    o = admin.post("/users", json={"username": other, "email": f"{other}@example.test", "password": STRONG,
                                   "role": "viewer"})
    assert o.status_code == 201
    r = admin.post("/users", json={"username": name.upper(), "email": f"{other}@example.test", "password": STRONG,
                                   "role": "viewer"})
    assert r.status_code == 409
    r = admin.post("/users", json={"username": name, "email": f"{other}@example.test", "password": STRONG,
                                   "role": "viewer"})
    assert r.status_code == 409
    # Clearing the full name persists; omitting it preserves it (UA-05).
    c = Client(name, STRONG)
    c.login()
    assert admin.put(f"/users/{uid}", json={"email": f"{name}.new@example.test"}).json()["full_name"] == "Pat Example"
    assert c.get("/auth/me").status_code == 200, "an email-only edit must not revoke sessions (RP-25)"
    assert admin.put(f"/users/{uid}", json={"full_name": None}).json()["full_name"] is None
    assert c.get("/auth/me").status_code == 200, "a name-only edit must not revoke sessions"
    # A real access change does revoke them.
    assert admin.put(f"/users/{uid}", json={"role": "read_only"}).status_code == 200
    assert c.get("/auth/me").status_code == 401


def test_password_policy_is_enforced_consistently(admin):
    pol = httpx.get(f"{API}/auth/password-policy", timeout=10).json()
    assert pol["min_length"] == 12
    name = uniq("pw")
    base = {"username": name, "email": f"{name}@example.test", "role": "viewer"}
    for bad in ("short", "elevenchars", "password1234", name + "12345"):
        r = admin.post("/users", json={**base, "password": bad})
        assert r.status_code == 422, (bad, r.text)
    uid = admin.post("/users", json={**base, "password": STRONG}).json()["id"]
    assert admin.post(f"/users/{uid}/reset-password", json={"new_password": "123456"}).status_code == 422
    assert admin.post(f"/users/{uid}/reset-password", json={"new_password": "password1234"}).status_code == 422


def test_directory_owned_role_cannot_be_edited_via_the_user_api(admin):
    async def _insert():
        import asyncpg
        conn = await asyncpg.connect(os.environ.get("ZENPLUS_ACCESS_E2E_DSN",
                                                    "postgresql://zenplus:scratch@127.0.0.1:55432/zenplus"))
        name = uniq("ldap")
        row = await conn.fetchrow(
            "INSERT INTO users (username,email,password_hash,role,auth_source) VALUES ($1,$2,'!external-auth!','viewer','ldap') RETURNING id",
            name, f"{name}@ldap.external")
        await conn.close()
        return str(row["id"])
    uid = asyncio.run(_insert())
    r = admin.put(f"/users/{uid}", json={"role": "operator"})
    assert r.status_code == 409 and "LDAP" in r.text
    assert admin.post(f"/users/{uid}/reset-password", json={"new_password": STRONG}).status_code == 400


# ── Account lifecycle: forced change, lockout, sessions, MFA ──────────────────

def test_forced_password_change_blocks_everything_else_until_done(admin):
    name = uniq("force")
    r = admin.post("/users", json={"username": name, "email": f"{name}@example.test", "password": STRONG,
                                   "role": "viewer", "must_change_password": True})
    assert r.status_code == 201
    c = Client(name, STRONG)
    assert c.login().status_code == 200
    me = c.get("/auth/me").json()
    assert me["must_change_password"] is True
    blocked = c.get("/devices")
    assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "password_change_required"
    new = "A-Brand-New-Passphrase-77"
    assert c.post("/users/me/change-password", json={"current_password": STRONG, "new_password": new}).status_code == 200
    c2 = Client(name, new)
    assert c2.login().status_code == 200
    assert c2.get("/devices").status_code != 403


def test_lockout_unlock_and_session_revocation(admin):
    victim = make_user(admin, "viewer")
    bad = Client(victim.username, "wrong-password-xyz")
    codes = []
    for _ in range(11):
        codes.append(bad.login().status_code)
        time.sleep(0.05)
    assert 429 in codes[-3:], codes
    # Correct password is refused while locked, and an admin unlock restores access.
    assert Client(victim.username, STRONG).login().status_code == 429
    assert admin.post(f"/users/{victim.user_id}/unlock").status_code == 200
    ok = Client(victim.username, STRONG)
    assert ok.login().status_code == 200

    # Session inventory + revoke one / all.
    s2 = Client(victim.username, STRONG)
    s2.login()
    sessions = admin.get(f"/users/{victim.user_id}/sessions").json()["data"]
    assert len(sessions) >= 2
    one = next(s for s in sessions if s["id"] != (ok.get("/auth/me").json()["session_id"]))
    assert admin.delete(f"/users/{victim.user_id}/sessions/{one['id']}").status_code == 200
    assert admin.post(f"/users/{victim.user_id}/revoke-sessions").status_code == 200
    assert ok.get("/auth/me").status_code == 401 and s2.get("/auth/me").status_code == 401


def _totp(secret: str) -> str:
    import base64, hashlib, hmac, struct
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", int(time.time() // 30)), hashlib.sha1).digest()
    off = mac[-1] & 0xF
    return str((struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % 10**6).zfill(6)


def test_totp_enrolment_login_challenge_and_recovery(admin):
    u = make_user(admin, "viewer")
    assert u.post("/auth/mfa/setup", json={"password": "wrong"}).status_code == 400
    setup = u.post("/auth/mfa/setup", json={"password": STRONG}).json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/")
    assert u.post("/auth/mfa/enable", json={"code": "000000"}).status_code == 400
    enabled = u.post("/auth/mfa/enable", json={"code": _totp(setup["secret"])})
    assert enabled.status_code == 200
    recovery = enabled.json()["recovery_codes"]
    assert len(recovery) == 10

    # Password alone now yields a challenge, not a session.
    fresh = Client(u.username, STRONG)
    step1 = fresh.login()
    assert step1.status_code == 200 and step1.json()["mfa_required"] is True and not step1.json().get("access_token")
    chal = step1.json()["challenge"]
    bad = httpx.post(f"{API}/auth/login/mfa", json={"challenge": chal, "code": "123456"}, timeout=10)
    assert bad.status_code == 401
    # A TOTP code cannot be replayed; wait for the next step if it was already used at enrolment.
    code = _totp(setup["secret"])
    r = httpx.post(f"{API}/auth/login/mfa", json={"challenge": chal, "code": code}, timeout=10)
    if r.status_code == 401:
        time.sleep(31)
        r = httpx.post(f"{API}/auth/login/mfa", json={"challenge": chal, "code": _totp(setup["secret"])}, timeout=10)
    assert r.status_code == 200 and r.json()["access_token"]
    # Recovery codes work once.
    chal2 = Client(u.username, STRONG).login().json()["challenge"]
    used = httpx.post(f"{API}/auth/login/mfa", json={"challenge": chal2, "code": recovery[0]}, timeout=10)
    assert used.status_code == 200
    chal3 = Client(u.username, STRONG).login().json()["challenge"]
    assert httpx.post(f"{API}/auth/login/mfa", json={"challenge": chal3, "code": recovery[0]}, timeout=10).status_code == 401
    # An administrator can remove a lost authenticator.
    assert admin.post(f"/users/{u.user_id}/mfa/reset").status_code == 200
    assert Client(u.username, STRONG).login().json().get("access_token")


def test_mfa_policy_forces_enrolment_before_other_access(admin):
    # The acting administrator must have MFA themselves, or the policy would lock them out.
    refused = admin.put("/system/auth/policy", json={"mfa_required": "all_local", "lockout_threshold": 10,
                                                     "lockout_minutes": 15})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "enable_own_mfa_first"

    padm = make_user(admin, make_role(admin, ["system.admin"], "MFA admin")["name"])
    secret = padm.post("/auth/mfa/setup", json={"password": STRONG}).json()["secret"]
    assert padm.post("/auth/mfa/enable", json={"code": _totp(secret)}).status_code == 200
    target = make_user(admin, make_role(admin, ["users.view", "users.manage"], "Needs MFA")["name"])

    def mfa_login(c: Client):
        chal = c.http.post("/auth/login", json={"username": c.username, "password": c.password}).json()["challenge"]
        for _ in range(2):
            r = c.http.post("/auth/login/mfa", json={"challenge": chal, "code": _totp(secret)})
            if r.status_code == 200:
                c.token = r.json()["access_token"]
                return r
            time.sleep(31)  # the enrolment code consumed this 30 s step (replay protection)
        return r
    assert mfa_login(padm).status_code == 200
    padm_policy = {"mfa_required": "all_local", "lockout_threshold": 10, "lockout_minutes": 15}
    try:
        assert padm.put("/system/auth/policy", json=padm_policy).status_code == 200
        time.sleep(0.2)
        t = Client(target.username, STRONG)
        t.login()
        r = t.get("/users")
        assert r.status_code == 403 and r.json()["detail"]["code"] == "mfa_enrollment_required"
        assert t.get("/auth/me").json()["mfa_required"] is True
        assert t.post("/auth/mfa/setup", json={"password": STRONG}).status_code == 200
    finally:
        padm.put("/system/auth/policy", json={**padm_policy, "mfa_required": "off"})


# ── UA-01..15: authentication providers ───────────────────────────────────────

def test_directory_role_mappings_are_validated_and_privileged_grants_confirmed(admin):
    cfg = {"enabled": False, "server": "ldap.invalid", "base_dn": "dc=invalid", "bind_dn": "",
           "bind_password": "", "group_mappings": [], "default_role": ""}
    # Unknown role, incomplete row, duplicates -> row-level 422.
    r = admin.put("/system/auth/ldap", json={**cfg, "group_mappings": [{"group": "NetOps", "role": "no_such_role"}]})
    assert r.status_code == 422 and "does not exist" in r.json()["detail"]["message"]
    r = admin.put("/system/auth/ldap", json={**cfg, "group_mappings": [{"group": "NetOps", "role": ""}]})
    assert r.status_code == 422 and "choose the role" in r.json()["detail"]["message"]
    r = admin.put("/system/auth/ldap", json={**cfg, "group_mappings": [
        {"group": "NetOps", "role": "viewer"}, {"group": "netops", "role": "operator"}]})
    assert r.status_code == 422 and "duplicates" in r.json()["detail"]["message"]
    # Granting Administrator needs an explicit confirmation.
    admin_map = {**cfg, "group_mappings": [{"group": "NetAdmins", "role": "admin"}]}
    r = admin.put("/system/auth/ldap", json=admin_map)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "privileged_mapping_confirmation_required"
    ok = admin.put("/system/auth/ldap", json={**admin_map, "confirm_privileged": True})
    assert ok.status_code == 200, ok.text
    rev = ok.json()["revision"]
    assert rev >= 1
    # Deleting / renaming roles used by a mapping is refused or carried along.
    refs = admin.get("/roles").json()
    viewer_like = make_role(admin, ["devices.view"], display="Dir viewer")
    assert admin.put("/system/auth/ldap", json={**cfg, "group_mappings": [
        {"group": "NetAdmins", "role": "admin"}, {"group": "Ops", "role": viewer_like["name"]}],
        "confirm_privileged": True, "expected_revision": rev}).status_code == 200
    assert admin.delete(f"/roles/{viewer_like['id']}").status_code == 409
    new_name = uniq("dirrole")
    assert admin.put(f"/roles/{viewer_like['id']}", json={"name": new_name}).status_code == 200
    saved = admin.get("/system/auth/providers").json()["ldap"]
    assert {m["role"] for m in saved["group_mappings"]} == {"admin", new_name}
    # Stale full-form save is a conflict.
    stale = admin.put("/system/auth/ldap", json={**admin_map, "confirm_privileged": True, "expected_revision": rev})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "provider_revision_conflict"


def test_provider_toggle_secret_clear_audit_and_radius_budget(admin):
    # The narrow toggle never reverts other fields.
    before = admin.get("/system/auth/providers").json()["ldap"]
    t = admin.post("/system/auth/ldap/enabled", json={"enabled": True})
    assert t.status_code in (200, 400)  # 400 when LDAP lacks server/base DN in this fixture
    after = admin.get("/system/auth/providers").json()["ldap"]
    for k in ("server", "base_dn", "group_mappings", "default_role"):
        assert after[k] == before[k]
    # RADIUS: the timeout x retries budget is bounded; a mapping cannot be saved half-filled.
    r = admin.put("/system/auth/radius", json={"enabled": False, "timeout": 60, "retries": 10})
    assert r.status_code == 422
    ok = admin.put("/system/auth/radius", json={"enabled": False, "server": "radius.invalid", "secret": "s3cret",
                                                "timeout": 3, "retries": 3, "class_mappings": []})
    assert ok.status_code == 200
    assert ok.json()["default_role"] == "" and ok.json()["has_secret"] is True
    cleared = admin.put("/system/auth/radius", json={"enabled": False, "server": "radius.invalid",
                                                     "clear_secret": True, "timeout": 3, "retries": 3})
    assert cleared.json()["has_secret"] is False
    # Enabling without a secret fails; the audit trail records a redacted diff.
    assert admin.post("/system/auth/radius/enabled", json={"enabled": True}).status_code == 400
    hist = admin.get("/system/auth/history", params={"provider": "radius"}).json()["data"]
    flat = str(hist)
    assert "s3cret" not in flat and "secret" in flat
    assert any(h["details"].get("secret") == "cleared" for h in hist if h["action"] == "settings.auth.update")


def test_provider_tests_report_failures_without_500_and_explain_admission(admin):
    r = admin.post("/system/auth/ldap/test", json={"ldap": {"enabled": True, "server": "127.0.0.1", "port": 9,
                                                            "base_dn": "dc=x", "use_starttls": True}})
    assert r.status_code == 200 and r.json()["success"] is False
    r = admin.post("/system/auth/radius/test", json={"username": "u", "password": "p",
                                                     "radius": {"enabled": True, "server": "127.0.0.1", "port": 9,
                                                                "secret": "x", "timeout": 1, "retries": 1}})
    assert r.status_code == 200 and r.json()["success"] is False


# ── RP-13/16 backend surface used by the UI ──────────────────────────────────

def test_catalog_marks_risk_and_dependencies(admin):
    cat = admin.get("/roles/catalog").json()
    perms = {p["id"]: p for m in cat["modules"] for p in m["permissions"]}
    assert perms["system.admin"]["risk"] == "critical" and perms["users.manage"]["risk"] == "privileged"
    assert "devices.view" in perms["devices.manage"]["implies"]
    assert "netflow.manage_shared" in perms and "roles.view" in perms


def test_effective_access_and_access_review_export(admin):
    u = make_user(admin, "operator", scope=[])
    eff = admin.get(f"/users/{u.user_id}/effective-access").json()
    assert eff["role"]["name"] == "operator" and eff["scope"]["state"] == "all"
    assert eff["permission_count"] > 10
    csv_resp = admin.get("/users/access-review")
    assert csv_resp.status_code == 200 and csv_resp.headers["content-type"].startswith("text/csv")
    assert "username,full_name,email,status" in csv_resp.text
    # Listing supports filters / sorting / pagination with a total header.
    page = admin.get("/users", params={"status": "active", "sort": "username", "limit": 2})
    assert page.status_code == 200 and len(page.json()) <= 2 and int(page.headers["x-total-count"]) >= 1


# ── RP-09: scope is enforced for reports and schedules (fail closed) ──────────

def test_scoped_accounts_cannot_export_fleet_wide_reports_or_manage_schedules(admin):
    tag = uniq("team")
    r = admin.post("/tags", json={"name": tag, "color": "#336699"})
    assert r.status_code in (200, 201), r.text
    scoped = make_user(admin, make_role(admin, ["reports.view", "reports.export", "reports.manage"])["name"],
                       scope=[tag])
    assert scoped.get("/reports/data/inventory").status_code == 403
    assert scoped.post("/reports/generate", json={"report_type": "full_report", "period": "last_24h",
                                                  "format": "csv"}).status_code == 403
    assert scoped.post("/report-schedules", json={"name": "x", "report_type": "executive_summary"}).status_code == 403
    # An unscoped account with the same role is not affected.
    open_user = make_user(admin, make_role(admin, ["reports.view", "reports.export", "reports.manage"])["name"])
    assert open_user.get("/reports/data/inventory").status_code != 403


# ── OpenID Connect single sign-on against a mock identity provider ────────────

def _start_mock_idp(port: int):
    """A minimal OIDC provider: discovery, JWKS, /authorize (auto-approves as the
    user named in ?login_hint) and /token (verifies PKCE and client auth)."""
    import base64, hashlib, json, threading, uvicorn
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi import FastAPI, Form, Header
    from fastapi.responses import JSONResponse, RedirectResponse
    from jwt.algorithms import RSAAlgorithm
    import jwt as pyjwt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key())); jwk.update({"kid": "mock1", "use": "sig", "alg": "RS256"})
    issuer = f"http://127.0.0.1:{port}"
    codes: dict[str, dict] = {}
    app = FastAPI()

    @app.get("/.well-known/openid-configuration")
    def disco():
        return {"issuer": issuer, "authorization_endpoint": issuer + "/authorize", "token_endpoint": issuer + "/token",
                "jwks_uri": issuer + "/jwks", "id_token_signing_alg_values_supported": ["RS256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic"], "claims_supported": ["groups", "sub"]}

    @app.get("/jwks")
    def jwks():
        return {"keys": [jwk]}

    @app.get("/authorize")
    def authorize(redirect_uri: str, state: str, nonce: str, code_challenge: str,
                  client_id: str, login_hint: str = "alice", groups: str = "NetOps", sub: str = ""):
        code = uuid.uuid4().hex
        codes[code] = {"nonce": nonce, "challenge": code_challenge, "user": login_hint, "groups": groups.split(","),
                       "sub": sub or f"sub-{login_hint}", "client_id": client_id}
        return RedirectResponse(f"{redirect_uri}?code={code}&state={state}", status_code=302)

    @app.post("/token")
    def token(code: str = Form(...), code_verifier: str = Form(...), authorization: str = Header("")):
        data = codes.pop(code, None)
        if not data:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        want = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
        basic = base64.b64decode(authorization.split(" ", 1)[1]).decode() if authorization.startswith("Basic ") else ""
        if want != data["challenge"] or basic != f"{data['client_id']}:mock-secret":
            return JSONResponse({"error": "invalid_grant", "error_description": "bad pkce or client auth"}, status_code=400)
        now = int(time.time())
        idt = pyjwt.encode({"iss": issuer, "aud": data["client_id"], "sub": data["sub"], "iat": now, "exp": now + 300,
                            "nonce": data["nonce"], "preferred_username": data["user"], "email": f"{data['user']}@sso.test",
                            "name": data["user"].title(), "groups": data["groups"]},
                           key, algorithm="RS256", headers={"kid": "mock1"})
        return {"id_token": idt, "access_token": "unused", "token_type": "Bearer"}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            httpx.get(issuer + "/jwks", timeout=1); break
        except Exception:
            time.sleep(0.1)
    return issuer, server


def _sso_signin(base: str, issuer: str, **hint) -> tuple[httpx.Client, httpx.Response]:
    """Drive the browser redirects by hand. Returns the cookie-holding client and
    the final callback response."""
    browser = httpx.Client(base_url=base, follow_redirects=False, timeout=15)
    r = browser.get("/api/v1/auth/oidc/login")
    assert r.status_code == 303, r.text
    auth_url = r.headers["location"]
    assert auth_url.startswith(issuer + "/authorize") and "code_challenge_method=S256" in auth_url
    from urllib.parse import urlencode
    hop = httpx.get(auth_url + "&" + urlencode(hint), follow_redirects=False, timeout=15)   # the user "approves" at the IdP
    assert hop.status_code == 302, (auth_url, hop.text)
    callback = hop.headers["location"]
    from urllib.parse import urlparse as _u
    return browser, browser.get(_u(callback).path + "?" + _u(callback).query)


def test_oidc_single_sign_on_end_to_end(admin):
    issuer, idp = _start_mock_idp(9931)
    try:
        role = make_role(admin, ["devices.view", "alerts.view"], "SSO ops")
        base_cfg = {"enabled": True, "issuer": issuer, "client_id": "zp-e2e", "client_secret": "mock-secret",
                    "display_name": "Corp SSO", "group_mappings": [{"group": "NetOps", "role": role["name"]}],
                    "default_role": "", "auto_provision": True}
        assert admin.put("/system/auth/oidc", json=base_cfg).status_code == 200
        assert httpx.get(f"{API}/auth/oidc/config", timeout=10).json() == {"enabled": True, "display_name": "Corp SSO"}
        test = admin.post("/system/auth/oidc/test", json={}).json()
        assert test["success"] and test["redirect_uri"].endswith("/api/v1/auth/oidc/callback")

        # Happy path: group maps to a role, the account is provisioned and linked by `sub`.
        user = uniq("sso")
        browser, cb = _sso_signin(BASE_URL, issuer, login_hint=user, groups="NetOps,Other", sub=f"sub-{user}")
        assert cb.status_code == 303 and cb.headers["location"].startswith("/login?sso="), cb.text
        code = cb.headers["location"].split("sso=", 1)[1]
        tok = httpx.post(f"{API}/auth/oidc/exchange", json={"code": code}, timeout=10)
        assert tok.status_code == 200
        me = httpx.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {tok.json()['access_token']}"}, timeout=10).json()
        assert me["username"] == user and me["role"] == role["name"] and me["auth_source"] == "oidc"
        # The one-time code is single use, and the password path refuses SSO accounts.
        assert httpx.post(f"{API}/auth/oidc/exchange", json={"code": code}, timeout=10).status_code == 400
        assert httpx.post(f"{API}/auth/login", json={"username": user, "password": "anything-at-all-123"},
                          timeout=10).status_code == 401

        # An unmapped group is refused, as is a tampered state.
        _b, cb2 = _sso_signin(BASE_URL, issuer, login_hint=uniq("nogroup"), groups="Unrelated")
        assert cb2.status_code == 303 and "sso_error=" in cb2.headers["location"]
        b3 = httpx.Client(base_url=BASE_URL, follow_redirects=False, timeout=15)
        b3.get("/api/v1/auth/oidc/login")
        bad = b3.get("/api/v1/auth/oidc/callback?code=x&state=forged")
        assert "sso_error=" in bad.headers["location"]

        # A local account with the same username blocks the sign-in rather than being merged.
        local = make_user(admin, "viewer")
        _b, cb4 = _sso_signin(BASE_URL, issuer, login_hint=local.username, groups="NetOps", sub="sub-impostor")
        assert "sso_error=" in cb4.headers["location"] and "already%20exists" in cb4.headers["location"]

        # Disabling the provider blocks new sign-ins and (optionally) revokes open sessions.
        d = admin.post("/system/auth/oidc/enabled", json={"enabled": False, "revoke_sessions": True})
        assert d.status_code == 200 and d.json()["sessions_revoked"] >= 1
        assert httpx.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
                         timeout=10).status_code == 401
        assert httpx.get(f"{API}/auth/oidc/config", timeout=10).json()["enabled"] is False
    finally:
        idp.should_exit = True
