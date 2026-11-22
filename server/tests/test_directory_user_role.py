"""The role of a directory account (LDAP / RADIUS / OIDC) can be set by hand (migrate-195).

Reported from the field: an account that signs in through Active Directory could
not have its role changed in Settings -> Users. The user API refused the change
(409) and every sign-in put the mapped role back.

The contract (``app.services.role_source``), on a disposable PostgreSQL and
through the real routes and the real sign-in path (only the directory call
itself is faked):

* the directory decides WHETHER the account may sign in; the mapping decides
  WHICH role it holds unless an administrator set one by hand;
* a hand-set role survives sign-in, and what the mapping gives is recorded
  beside it instead of being dropped;
* handing the account back applies the directory's role (at once when known);
* delegation still applies, and neighbouring fields behave: scope tags and the
  enabled state are kept across sign-ins, password actions stay refused.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from inventory_fleet import api_client, sessions
from pg_fixture import pg_cluster, pg_db_module  # noqa: F401  (fixtures)

GROUP_VIEW = "CN=ZP-Viewers,OU=Groups,DC=example,DC=test"
GROUP_OPS = "CN=ZP-Operators,OU=Groups,DC=example,DC=test"


@pytest.fixture(scope="module")
def db(pg_db_module):
    d = pg_db_module
    d.execute("INSERT INTO system_settings (key, value) VALUES ('auth.ldap', $1::jsonb)"
              " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
              {"enabled": True, "server": "ldap.example.test", "base_dn": "dc=example,dc=test",
               "auto_provision": True, "default_role": "",
               "group_mappings": [{"group": GROUP_OPS, "role": "operator"}, {"group": GROUP_VIEW, "role": "viewer"}]})
    return d


def directory_user(db, source="ldap", role="viewer", **cols) -> str:
    name = f"{source}{uuid.uuid4().hex[:8]}"
    fields = {"username": name, "email": f"{name}@{source}.external", "password_hash": "!external-auth!",
              "role": role, "auth_source": source, **cols}
    names = ", ".join(fields)
    marks = ", ".join(f"${i}" for i in range(1, len(fields) + 1))
    return str(db.scalar(f"INSERT INTO users ({names}) VALUES ({marks}) RETURNING id", *fields.values()))


def row(db, uid):
    r = db.query("SELECT username, role, role_manual, directory_role, is_active, scope_tags, full_name, email"
                 " FROM users WHERE id = $1::uuid", uid)[0]
    return dict(zip(("username", "role", "role_manual", "directory_role", "is_active", "scope_tags", "full_name",
                     "email"), r))


def admin(db):
    return api_client(db, role="admin", username="role-admin", persist_user=True)


def sign_in(db, monkeypatch, username, groups, *, full_name="Dir Name", email=None):
    """One LDAP sign-in through the real login path; only the directory answer is faked."""
    from app.api.v1 import auth

    def fake_ldap(cfg, user, password):
        return {"dn": f"CN={user},DC=example,DC=test", "groups": list(groups), "email": email, "full_name": full_name}

    monkeypatch.setattr(auth, "ldap_authenticate", fake_ldap)
    return login(db, username)


def login(db, username):
    from app.api.v1 import auth
    from app.schemas.auth import LoginRequest

    async def go():
        async with sessions(db) as factory:
            async with factory() as session:
                session.answers_role_lookup = True
                return await auth._login_impl(LoginRequest(username=username, password="directory-password"),
                                              session, None)
    return asyncio.run(go())


# ── the reported bug ─────────────────────────────────────────────────────────

def test_an_administrator_can_change_the_role_of_a_directory_user_and_it_survives_sign_in(db, monkeypatch):
    uid = directory_user(db)
    name = row(db, uid)["username"]
    with admin(db) as c:
        before = c.get(f"/api/v1/users/{uid}").json()
        assert before["role_source"] == "directory" and before["role"] == "viewer"

        r = c.put(f"/api/v1/users/{uid}", json={"role": "operator"})
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "operator" and r.json()["role_source"] == "manual"

    # The directory still maps this account to Viewer. The hand-set role stays,
    # and the mapped one is recorded rather than dropped.
    token = sign_in(db, monkeypatch, name, [GROUP_VIEW])
    assert token.user.role == "operator"
    assert row(db, uid) == {**row(db, uid), "role": "operator", "role_manual": True, "directory_role": "viewer"}

    # ... and at every later sign-in, whatever the directory maps then.
    sign_in(db, monkeypatch, name, [GROUP_OPS, GROUP_VIEW])
    assert row(db, uid)["role"] == "operator" and row(db, uid)["directory_role"] == "operator"

    with admin(db) as c:
        shown = c.get(f"/api/v1/users/{uid}/effective-access").json()
        assert shown["role"]["source"] == "manual" and shown["role"]["directory_role"] == "operator"
        audit = [h for h in shown["history"] if h["action"] == "user.update"]
        assert audit and audit[-1]["details"]["changes"]["role_source"]["to"] == "manual"


def test_a_directory_account_that_was_never_touched_still_follows_the_mapping(db, monkeypatch):
    uid = directory_user(db)
    name = row(db, uid)["username"]
    sign_in(db, monkeypatch, name, [GROUP_OPS])
    assert row(db, uid)["role"] == "operator" and row(db, uid)["role_manual"] is False
    assert row(db, uid)["directory_role"] == "operator"
    sign_in(db, monkeypatch, name, [GROUP_VIEW])
    assert row(db, uid)["role"] == "viewer"


def test_a_new_directory_account_is_provisioned_on_the_mapped_role(db, monkeypatch):
    name = f"fresh{uuid.uuid4().hex[:8]}"
    token = sign_in(db, monkeypatch, name, [GROUP_OPS], email=f"{name}@example.test")
    uid = str(token.user.id)
    assert row(db, uid)["role"] == "operator" and row(db, uid)["directory_role"] == "operator"
    with admin(db) as c:
        assert c.get(f"/api/v1/users/{uid}").json()["role_source"] == "directory"


def test_ldap_listed_accounts_use_the_role_assigned_in_users(db, monkeypatch):
    from fastapi import HTTPException
    from app.api.v1 import auth, auth_settings, users

    original = db.scalar("SELECT value FROM system_settings WHERE key = 'auth.ldap'")
    restricted = {**original, "auto_provision": False, "group_mappings": [], "default_role": ""}
    db.execute("UPDATE system_settings SET value = $1::jsonb WHERE key = 'auth.ldap'", restricted)
    name = f"listed{uuid.uuid4().hex[:8]}"
    data = {"username": name, "auth_source": "ldap", "role": "operator"}
    monkeypatch.setattr(users, "ldap_lookup_user", lambda *_: None)
    try:
        with admin(db) as c:
            assert c.post("/api/v1/users", json={**data, "auth_source": "local"}).status_code == 422
            assert c.post("/api/v1/users", json={**data, "password": "unused-local-password"}).status_code == 422
            r = c.post("/api/v1/users", json=data)
            assert r.status_code == 201, r.text
            account = r.json()
            assert (account["auth_source"], account["role_source"], account["role"]) == ("ldap", "manual", "operator")

            directory_answer = {"dn": f"CN={name},DC=example,DC=test", "groups": [], "email": None, "full_name": None}
            monkeypatch.setattr(auth_settings, "ldap_authenticate", lambda *_: directory_answer)
            result = c.post("/api/v1/system/auth/ldap/test", json={"username": name, "password": "directory-password"})
            assert result.status_code == 200, result.text
            assert result.json()["would_sign_in"] and result.json()["account_role"] == "operator"

            assert sign_in(db, monkeypatch, name.upper(), []).user.role == "operator"
            assert row(db, account["id"])["directory_role"] is None

            def directory_must_not_be_called(*_):
                pytest.fail("unlisted names must never reach LDAP in listed-accounts mode")

            monkeypatch.setattr(auth, "ldap_authenticate", directory_must_not_be_called)
            with pytest.raises(HTTPException) as denied:
                login(db, f"unlisted-{name}")
            assert denied.value.status_code == 401
            assert db.scalar("SELECT count(*) FROM users WHERE username = $1", f"unlisted-{name}") == 0

            assert c.put(f"/api/v1/users/{account['id']}", json={"is_active": False}).status_code == 200
            with pytest.raises(HTTPException) as denied:
                sign_in(db, monkeypatch, name, [])
            assert denied.value.status_code == 403 and "disabled" in denied.value.detail
    finally:
        db.execute("UPDATE system_settings SET value = $1::jsonb WHERE key = 'auth.ldap'", original)


def test_ldap_profile_is_filled_on_create_and_can_lose_its_email(db, monkeypatch):
    from app.api.v1 import users

    name = f"adprofile{uuid.uuid4().hex[:8]}"
    email = f"{name}@example.test"
    monkeypatch.setattr(users, "ldap_lookup_user", lambda *_: {
        "full_name": "Directory Name", "email": email.upper(),
    })
    with admin(db) as c:
        data = {"username": name, "auth_source": "ldap", "role": "viewer"}
        response = c.post("/api/v1/users", json=data)
        assert response.status_code == 201, response.text
        created = response.json()
        assert created["full_name"] == "Directory Name" and created["email"] == email

        monkeypatch.setattr(users, "ldap_lookup_user", lambda *_: {"full_name": "Another Account", "email": email})
        response = c.post("/api/v1/users", json={**data, "username": f"{name}-other"})
        assert response.status_code == 201, response.text
        assert response.json()["full_name"] == "Another Account" and response.json()["email"] is None

        token = sign_in(db, monkeypatch, name, [GROUP_VIEW], full_name="Updated Name", email=None)
        assert token.user.role == "viewer" and token.user.email is None
        assert row(db, created["id"])["email"] is None
        assert row(db, created["id"])["full_name"] == "Updated Name"

        response = c.post("/api/v1/users", json={**data, "auth_source": "local", "username": f"{name}-local",
                                                "password": "L3mon-River-!Swan"})
        assert response.status_code == 422 and "email address is required" in response.json()["detail"]


# ── the directory still decides who gets in ──────────────────────────────────

def test_a_hand_set_role_does_not_admit_an_account_the_directory_no_longer_maps(db, monkeypatch):
    from fastapi import HTTPException

    uid = directory_user(db, role="operator", role_manual=True)
    with pytest.raises(HTTPException) as denied:
        sign_in(db, monkeypatch, row(db, uid)["username"], ["CN=Somebody Else,DC=example,DC=test"])
    assert denied.value.status_code == 403 and "no ZenPlus role is mapped" in denied.value.detail


def test_a_disabled_directory_account_stays_disabled_whatever_the_directory_says(db, monkeypatch):
    from fastapi import HTTPException

    uid = directory_user(db)
    with admin(db) as c:
        assert c.put(f"/api/v1/users/{uid}", json={"is_active": False}).status_code == 200
    with pytest.raises(HTTPException) as denied:
        sign_in(db, monkeypatch, row(db, uid)["username"], [GROUP_OPS])
    assert denied.value.status_code == 403 and "disabled" in denied.value.detail
    assert row(db, uid)["is_active"] is False


def test_a_stale_mapping_blocks_a_mapped_account_but_not_one_with_a_hand_set_role(db, monkeypatch):
    from fastapi import HTTPException

    stale = db.scalar("SELECT value FROM system_settings WHERE key = 'auth.ldap'")
    stale = {**stale, "group_mappings": [{"group": GROUP_OPS, "role": "role_that_was_deleted"}]}
    original = db.scalar("SELECT value FROM system_settings WHERE key = 'auth.ldap'")
    db.execute("UPDATE system_settings SET value = $1::jsonb WHERE key = 'auth.ldap'", stale)
    try:
        mapped = directory_user(db)
        with pytest.raises(HTTPException) as denied:
            sign_in(db, monkeypatch, row(db, mapped)["username"], [GROUP_OPS])
        assert "no longer exists" in denied.value.detail

        by_hand = directory_user(db, role="viewer", role_manual=True)
        assert sign_in(db, monkeypatch, row(db, by_hand)["username"], [GROUP_OPS]).user.role == "viewer"
    finally:
        db.execute("UPDATE system_settings SET value = $1::jsonb WHERE key = 'auth.ldap'", original)


# ── handing the role back, and keeping a role in place ───────────────────────

def test_handing_the_role_back_applies_what_the_directory_gives(db, monkeypatch):
    uid = directory_user(db, role="operator", role_manual=True, directory_role="viewer")
    with admin(db) as c:
        r = c.put(f"/api/v1/users/{uid}", json={"role_source": "directory"})
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "viewer" and r.json()["role_source"] == "directory"
    sign_in(db, monkeypatch, row(db, uid)["username"], [GROUP_OPS])
    assert row(db, uid)["role"] == "operator"          # the mapping is back in charge


def test_handing_back_before_the_directory_role_is_known_keeps_the_role_until_sign_in(db, monkeypatch):
    uid = directory_user(db, role="operator", role_manual=True)
    db.execute("INSERT INTO auth_sessions (id, user_id, expires_at) VALUES ($1::uuid, $2::uuid, NOW() + interval '1 hour')",
               str(uuid.uuid4()), uid)
    with admin(db) as c:
        r = c.put(f"/api/v1/users/{uid}", json={"role_source": "directory"})
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "operator" and r.json()["role_source"] == "directory"
    # The account is signed out, so the hand-set role cannot outlive the decision.
    assert db.scalar("SELECT count(*) FROM auth_sessions WHERE user_id = $1::uuid AND revoked_at IS NULL", uid) == 0
    sign_in(db, monkeypatch, row(db, uid)["username"], [GROUP_VIEW])
    assert row(db, uid)["role"] == "viewer"


def test_the_current_role_can_be_kept_by_hand_without_changing_it(db, monkeypatch):
    uid = directory_user(db, role="viewer")
    with admin(db) as c:
        r = c.put(f"/api/v1/users/{uid}", json={"role_source": "manual"})
        assert r.status_code == 200 and r.json()["role"] == "viewer" and r.json()["role_source"] == "manual"
    sign_in(db, monkeypatch, row(db, uid)["username"], [GROUP_OPS])
    assert row(db, uid)["role"] == "viewer" and row(db, uid)["directory_role"] == "operator"


def test_contradictory_and_meaningless_requests_are_refused(db):
    uid = directory_user(db, role="viewer")
    local = str(db.scalar("INSERT INTO users (username, email, password_hash, role) VALUES ($1, $2, '!', 'viewer')"
                          " RETURNING id", f"loc{uuid.uuid4().hex[:8]}", f"loc{uuid.uuid4().hex[:8]}@example.test"))
    with admin(db) as c:
        assert c.put(f"/api/v1/users/{uid}", json={"role": "operator", "role_source": "directory"}).status_code == 400
        assert c.put(f"/api/v1/users/{uid}", json={"role_source": "sometimes"}).status_code == 422
        assert c.put(f"/api/v1/users/{uid}", json={"role": "no_such_role"}).status_code == 400
        assert row(db, uid)["role_manual"] is False
        # A local account has no directory; its role is always set by hand.
        assert c.put(f"/api/v1/users/{local}", json={"role_source": "directory"}).status_code == 400
        r = c.put(f"/api/v1/users/{local}", json={"role": "operator", "role_source": "manual"})
        assert r.status_code == 200 and r.json()["role_source"] == "local"
        assert row(db, local)["role_manual"] is False


# ── delegation ───────────────────────────────────────────────────────────────

def test_a_delegated_user_manager_cannot_use_the_role_source_to_raise_access(db):
    manager_perms = ["users.view", "users.manage", "devices.view", "dashboard.view", "alerts.view"]
    # The directory gives this account Administrator; it currently holds a small hand-set role.
    small = "t_dir_small"
    with api_client(db, role="t_dir_manager", permissions=manager_perms, username="dir-manager",
                    persist_user=True) as c:
        from inventory_fleet import ensure_role
        ensure_role(db, small, ["devices.view"])
        above = directory_user(db, role=small, role_manual=True, directory_role="admin")
        r = c.put(f"/api/v1/users/{above}", json={"role_source": "directory"})
        assert r.status_code == 403, r.text
        unknown = directory_user(db, role=small, role_manual=True)
        r = c.put(f"/api/v1/users/{unknown}", json={"role_source": "directory"})
        assert r.status_code == 403 and "full administrator" in r.text
        # ... and cannot hand out a role above its own on a directory account either.
        plain = directory_user(db, role=small)
        assert c.put(f"/api/v1/users/{plain}", json={"role": "admin"}).status_code == 403
        assert row(db, above)["role"] == small and row(db, above)["role_manual"] is True


# ── neighbouring fields ──────────────────────────────────────────────────────

def test_scope_tags_set_by_hand_survive_sign_in_and_password_actions_stay_refused(db, monkeypatch):
    db.execute("INSERT INTO tags (name) VALUES ('dir-team-a') ON CONFLICT DO NOTHING")
    uid = directory_user(db)
    name = row(db, uid)["username"]
    with admin(db) as c:
        r = c.put(f"/api/v1/users/{uid}", json={"scope_tags": ["dir-team-a"]})
        assert r.status_code == 200 and r.json()["scope_tags"] == ["dir-team-a"]
        # Password actions make no sense for an account whose password lives in the directory.
        assert c.put(f"/api/v1/users/{uid}", json={"must_change_password": True}).status_code == 400
        assert c.post(f"/api/v1/users/{uid}/reset-password",
                      json={"new_password": "Correct-Horse-Battery-Staple-9"}).status_code == 400
    sign_in(db, monkeypatch, name, [GROUP_VIEW], full_name="From Directory", email=f"{name}@corp.example.test")
    after = row(db, uid)
    assert after["scope_tags"] == ["dir-team-a"]
    # Name and address are the directory's: it refreshes them when it supplies them.
    assert after["full_name"] == "From Directory" and after["email"] == f"{name}@corp.example.test"


def test_renaming_a_role_carries_the_recorded_directory_role(db):
    from inventory_fleet import ensure_role
    ensure_role(db, "t_dir_rename", ["devices.view"])
    uid = directory_user(db, role="viewer", role_manual=True, directory_role="t_dir_rename")
    with admin(db) as c:
        rid = next(r["id"] for r in c.get("/api/v1/roles").json() if r["name"] == "t_dir_rename")
        r = c.put(f"/api/v1/roles/{rid}", json={"name": "t_dir_renamed"})
        assert r.status_code == 200, r.text
    assert row(db, uid)["directory_role"] == "t_dir_renamed"


def test_access_review_export_states_who_set_each_role(db):
    by_hand = directory_user(db, role="operator", role_manual=True, directory_role="viewer")
    with admin(db) as c:
        csv_text = c.get("/api/v1/users/access-review").text
    header, *lines = csv_text.splitlines()
    assert header.endswith("role_set_by,directory_role")
    line = next(l for l in lines if l.startswith(row(db, by_hand)["username"] + ","))
    assert line.endswith("administrator (directory account),viewer")


# ── single sign-on (OIDC) follows the same rule ──────────────────────────────

def test_oidc_sign_in_keeps_a_hand_set_role_and_records_the_mapped_one(db):
    from app.api.v1 import auth

    sub = f"sub-{uuid.uuid4().hex}"
    uid = directory_user(db, source="oidc", role="operator", role_manual=True, external_id=sub)
    plain_sub = f"sub-{uuid.uuid4().hex}"
    plain = directory_user(db, source="oidc", role="operator", external_id=plain_sub)

    async def go(subject):
        async with sessions(db) as factory:
            async with factory() as session:
                ident = {"sub": subject, "username": "ignored", "email": None, "full_name": None, "groups": []}
                user = await auth._oidc_user(session, {"auto_provision": False}, ident, "viewer")
                await session.commit()
                return user.role

    assert asyncio.run(go(sub)) == "operator"
    assert row(db, uid)["directory_role"] == "viewer" and row(db, uid)["role_manual"] is True
    assert asyncio.run(go(plain_sub)) == "viewer"
    assert row(db, plain)["role"] == "viewer"

def test_username_rule_is_an_admission_list_even_after_first_login(db, monkeypatch):
    from fastapi import HTTPException
    from app.api.v1 import auth

    original = db.scalar("SELECT value FROM system_settings WHERE key = 'auth.ldap'")
    name = f"mapped{uuid.uuid4().hex[:8]}"
    cfg = {**original, "auto_provision": False, "default_role": "viewer",
           "group_mappings": [{"group": name, "kind": "username", "role": "operator"}]}
    sql = "UPDATE system_settings SET value = $1::jsonb WHERE key = 'auth.ldap'"
    try:
        db.execute(sql, cfg)
        info = {"dn": f"CN={name}", "groups": [], "email": None, "full_name": name, "ad_username": "different"}
        monkeypatch.setattr(auth, "ldap_authenticate", lambda *_: info)
        with pytest.raises(HTTPException):
            login(db, name)
        info["ad_username"] = name
        assert login(db, name.upper()).user.role == "operator"
        cfg["group_mappings"] = []
        db.execute(sql, cfg)
        with pytest.raises(HTTPException):
            login(db, name.upper())
    finally:
        db.execute(sql, original)
