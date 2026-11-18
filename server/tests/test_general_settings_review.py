"""Settings -> General review (2026-11-18): one regression test per bug fixed.

Real routes on a disposable PostgreSQL; nothing here talks to a mail server, a
directory, systemd or the licensing portal (those calls are faked at the edge).

* SMTP / Email edited a stale copy (the legacy settings key) and its Save then
  overwrote the gateway that actually sends mail; it returned the password to
  the browser; "Send test" tested the saved values, not the form.
* Company was shown to settings.manage but loaded with settings.view.
* Sign-in matched usernames by exact case, so a case variant of a disabled or
  scoped directory account was provisioned as a second, unrestricted account.
* An administrator could remove their own MFA from the users list without the
  password and code the profile asks for.
* A new "everyone else becomes Administrator" default slipped past the
  privileged-mapping confirmation when one rule already mapped to Administrator.
* Disabling a sign-in source could leave no administrator able to sign in.
* Saving any Updates setting restarted the updater timer, starting one that had
  been stopped on purpose.
* The licensing portal's 401 was relayed as ours and signed the administrator out.
* The MIB Library tab (settings.manage) sat on routes that wanted device permissions.
* Effective access listed permissions a page-limited role does not hold.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from inventory_fleet import aid, api_client, ensure_role, sessions
from pg_fixture import pg_cluster, pg_db_module  # noqa: F401  (fixtures)


@pytest.fixture(scope="module")
def db(pg_db_module):
    return pg_db_module


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    """Gateway configuration is encrypted at rest; the test process has no appliance key."""
    from app.core import crypto
    monkeypatch.setattr(crypto, "get_settings", lambda: SimpleNamespace(SNMP_ENC_KEY="a" * 64))


def admin(db):
    return api_client(db, role="admin", username="review-admin", persist_user=True)


def gateways(c) -> list[dict]:
    return [g for g in c.get("/api/v1/settings/gateways/list").json()["data"] if g["type"] == "smtp"]


# ── SMTP / Email ─────────────────────────────────────────────────────────────

SMTP = {"host": "old.example.test", "port": 587, "username": "alerts", "password": "old-secret",
        "from_email": "alerts@example.test", "from_name": "ZenPlus Alerts", "encryption": "tls", "enabled": True}


def test_smtp_tab_shows_and_saves_the_gateway_in_use_without_exposing_the_password(db):
    with admin(db) as c:
        assert c.put("/api/v1/settings/gateways/smtp", json=SMTP).status_code == 200
        gw = gateways(c)[0]
        # The administrator renames the gateway and rotates its server and password on the Gateways page.
        r = c.put(f"/api/v1/settings/gateways/{gw['id']}", json={
            "name": "Office365", "config": {**SMTP, "host": "live.example.test", "password": "live-secret"}})
        assert r.status_code == 200, r.text

        shown = c.get("/api/v1/settings/gateways").json()["smtp"]
        assert shown["host"] == "live.example.test" and shown["gateway_name"] == "Office365"
        assert shown["password"] == "" and shown["password_set"] is True
        assert "live-secret" not in c.get("/api/v1/settings/gateways").text

        # Changing only the display name must not put the old server or password back, or rename the gateway.
        form = {**{k: shown[k] for k in SMTP if k != "password"}, "password": "", "from_name": "NOC"}
        assert c.put("/api/v1/settings/gateways/smtp", json=form).status_code == 200
        after = gateways(c)
        assert len(after) == 1 and after[0]["name"] == "Office365" and after[0]["is_default"] is True
        assert after[0]["config"]["host"] == "live.example.test"
        assert after[0]["config"]["password"] == "live-secret" and after[0]["config"]["from_name"] == "NOC"

        # The password is removed only on purpose.
        assert c.put("/api/v1/settings/gateways/smtp", json={**form, "clear_password": True}).status_code == 200
        assert gateways(c)[0]["config"]["password"] == ""
        assert c.get("/api/v1/settings/gateways").json()["smtp"]["password_set"] is False


def test_smtp_settings_are_validated(db):
    with admin(db) as c:
        for bad in ({"port": 70000}, {"port": 0}, {"encryption": "starttls"}, {"host": "", "enabled": True},
                    {"from_email": "", "enabled": True}, {"from_email": "not an address"}):
            assert c.put("/api/v1/settings/gateways/smtp", json={**SMTP, **bad}).status_code == 422, bad
        # A disabled, half-filled form is a draft, not an error.
        assert c.put("/api/v1/settings/gateways/smtp",
                     json={**SMTP, "host": "", "enabled": False}).status_code == 200


def test_send_test_uses_the_form_values_with_the_stored_password(db, monkeypatch):
    from app.api.v1 import settings as settings_api

    seen = []
    monkeypatch.setattr(settings_api, "_do_smtp_test",
                        lambda cfg, recipient, regional=None: seen.append((dict(cfg), recipient)) or {"message": "sent"})
    with admin(db) as c:
        assert c.put("/api/v1/settings/gateways/smtp", json={**SMTP, "password": "kept-secret"}).status_code == 200
        typed = {**SMTP, "host": "typed.example.test", "password": ""}
        r = c.post("/api/v1/settings/gateways/smtp/test", json={"recipient": "me@example.test", "config": typed})
        assert r.status_code == 200, r.text
        assert seen[-1][0]["host"] == "typed.example.test" and seen[-1][0]["password"] == "kept-secret"
        # Without form values (older clients) the stored configuration is tested.
        assert c.post("/api/v1/settings/gateways/smtp/test", json={"recipient": "me@example.test"}).status_code == 200
        assert seen[-1][0]["host"] == SMTP["host"]


# ── Company ──────────────────────────────────────────────────────────────────

def test_whoever_may_save_the_company_tab_may_load_it(db):
    with api_client(db, role="t_rev_settings_manager", permissions=["settings.manage"], username="rev-settings") as c:
        assert c.get("/api/v1/settings/company").status_code == 200
    with api_client(db, role="t_rev_nobody", permissions=["devices.view"], username="rev-nobody") as c:
        assert c.get("/api/v1/settings/company").status_code == 403


# ── Users & Access ───────────────────────────────────────────────────────────

def _login(db, username, password):
    from app.api.v1 import auth
    from app.schemas.auth import LoginRequest

    async def go():
        async with sessions(db) as factory:
            async with factory() as session:
                session.answers_role_lookup = True
                return await auth._login_impl(LoginRequest(username=username, password=password), session, None)
    return asyncio.run(go())


def test_sign_in_resolves_usernames_ignoring_case_and_never_creates_a_case_variant(db, monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import auth
    from app.core.security import hash_password

    db.execute("INSERT INTO system_settings (key, value) VALUES ('auth.ldap', $1::jsonb)"
               " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
               {"enabled": True, "server": "ldap.example.test", "base_dn": "dc=example,dc=test",
                "auto_provision": True, "default_role": "operator", "group_mappings": []})
    monkeypatch.setattr(auth, "ldap_authenticate", lambda cfg, user, password: {
        "dn": f"CN={user}", "groups": [], "email": None, "full_name": None})
    tail = uuid.uuid4().hex[:8]
    name = f"jsmith{tail}"
    db.execute("INSERT INTO users (username, email, password_hash, role, auth_source, is_active)"
               " VALUES ($1, $2, '!external-auth!', 'viewer', 'ldap', false)", name, f"{name}@ldap.external")

    # The directory accepts "JSmith..." for the same person. The disabled account must answer, not a new one.
    with pytest.raises(HTTPException) as denied:
        _login(db, name.replace("jsmith", "JSmith"), "directory-password")
    assert denied.value.status_code == 403 and "disabled" in denied.value.detail
    assert db.scalar("SELECT count(*) FROM users WHERE lower(username) = $1", name) == 1

    local = f"Local{tail}"
    db.execute("INSERT INTO users (username, email, password_hash, role) VALUES ($1, $2, $3, 'viewer')",
               local, f"{local.lower()}@example.test", hash_password("Correct-Horse-Battery-Staple-9"))
    assert _login(db, local.lower(), "Correct-Horse-Battery-Staple-9").user.username == local

    # The database refuses a case variant too (migrate-195).
    with pytest.raises(Exception, match="users_username_lower_key"):
        db.execute("INSERT INTO users (username, email, password_hash, role) VALUES ($1, $2, '!', 'viewer')",
                   local.upper(), f"{local.lower()}-2@example.test")


def test_an_administrator_cannot_remove_their_own_mfa_from_the_users_list(db):
    with admin(db) as c:
        own = aid("user", "review-admin")
        r = c.post(f"/api/v1/users/{own}/mfa/reset")
        assert r.status_code == 400 and "profile" in r.text
        other = db.scalar("INSERT INTO users (username, email, password_hash, role, mfa_enabled) VALUES ($1, $2, '!',"
                          " 'viewer', true) RETURNING id", f"mfa{uuid.uuid4().hex[:8]}", f"mfa{uuid.uuid4().hex[:8]}@example.test")
        assert c.post(f"/api/v1/users/{other}/mfa/reset").status_code == 200


def test_effective_access_shows_what_a_page_limited_role_actually_holds(db):
    ensure_role(db, "t_rev_wall", ["devices.view", "alerts.view", "reports.view"])
    uid = db.scalar("INSERT INTO users (username, email, password_hash, role) VALUES ($1, $2, '!', 't_rev_wall')"
                    " RETURNING id", f"wall{uuid.uuid4().hex[:8]}", f"wall{uuid.uuid4().hex[:8]}@example.test")
    with admin(db) as c:
        unlimited = c.get(f"/api/v1/users/{uid}/effective-access").json()
        db.execute("UPDATE roles SET page_access = $1::jsonb WHERE name = 't_rev_wall'", {"pages": ["/devices"], "maps": None})
        limited = c.get(f"/api/v1/users/{uid}/effective-access").json()
    held = lambda body: {p["id"] for m in body["permissions"] for p in m["permissions"] if p["granted"]}  # noqa: E731
    assert {"devices.view", "alerts.view", "reports.view"} <= held(unlimited)
    assert "devices.view" in held(limited) and "reports.view" not in held(limited)


# ── Authentication ───────────────────────────────────────────────────────────

LDAP = {"enabled": False, "server": "ldap.invalid", "port": 636, "use_ssl": True, "use_starttls": False,
        "base_dn": "dc=invalid", "bind_dn": "", "bind_password": "", "group_mappings": [], "default_role": ""}


def test_every_new_privileged_grant_is_confirmed_not_only_a_new_role_name(db):
    with admin(db) as c:
        one_group = {**LDAP, "group_mappings": [{"group": "ZenAdmins", "role": "admin"}]}
        assert c.put("/api/v1/system/auth/ldap", json=one_group).status_code == 409
        saved = c.put("/api/v1/system/auth/ldap", json={**one_group, "confirm_privileged": True})
        assert saved.status_code == 200, saved.text
        rev = saved.json()["revision"]
        # Saving the same configuration again asks nothing.
        again = c.put("/api/v1/system/auth/ldap", json={**one_group, "expected_revision": rev})
        assert again.status_code == 200, again.text
        rev = again.json()["revision"]

        # "Everyone the rules do not match becomes Administrator" is a new grant of an already-used role.
        everyone = {**one_group, "default_role": "admin", "expected_revision": rev}
        r = c.put("/api/v1/system/auth/ldap", json=everyone)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "privileged_mapping_confirmation_required"
        another_group = {**one_group, "expected_revision": rev,
                         "group_mappings": one_group["group_mappings"] + [{"group": "Helpdesk", "role": "admin"}]}
        assert c.put("/api/v1/system/auth/ldap", json=another_group).status_code == 409
        assert c.put("/api/v1/system/auth/ldap", json={**another_group, "confirm_privileged": True}).status_code == 200


def test_a_source_every_administrator_uses_cannot_be_disabled(db):
    db.execute("INSERT INTO system_settings (key, value) VALUES ('auth.ldap', $1::jsonb)"
               " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
               {**LDAP, "enabled": True, "default_role": "viewer"})
    locals_ = [r[0] for r in db.query("SELECT id FROM users WHERE role = 'admin' AND auth_source = 'local'")]
    with admin(db) as c:
        db.execute("UPDATE users SET auth_source = 'ldap' WHERE role = 'admin'")
        try:
            r = c.post("/api/v1/system/auth/ldap/enabled", json={"enabled": False, "revoke_sessions": True})
            assert r.status_code == 409 and r.json()["detail"]["code"] == "no_administrator_left"
            assert c.get("/api/v1/system/auth/providers").json()["ldap"]["enabled"] is True
        finally:
            db.execute("UPDATE users SET auth_source = 'local' WHERE id = ANY($1::uuid[])", locals_)
        # With a local administrator to fall back on, it is allowed.
        assert c.post("/api/v1/system/auth/ldap/enabled", json={"enabled": False}).status_code == 200
        providers = c.get("/api/v1/system/auth/providers").json()
        assert providers["ldap"]["enabled"] is False and providers["local"]["accounts"] >= len(locals_)


def test_provider_test_does_not_use_a_secret_that_is_about_to_be_removed():
    from app.api.v1.auth_settings import LdapConfigIn, _with_override

    stored = {**LDAP, "bind_password": "stored-secret"}
    keep = _with_override(stored, LdapConfigIn(**LDAP), "bind_password")
    assert keep["bind_password"] == "stored-secret"
    removed = _with_override(stored, LdapConfigIn(**LDAP, clear_secret=True), "bind_password")
    assert removed["bind_password"] == ""


def test_a_failed_directory_call_is_not_reported_as_a_working_connection():
    from app.api.v1.auth_settings import _failed_before_credentials

    assert _failed_before_credentials([{"name": "connection", "ok": True}]) == [{"name": "connection", "ok": False}]
    reached = [{"name": "connection", "ok": True}, {"name": "credentials", "ok": False}]
    assert _failed_before_credentials(reached) == reached


# ── Updates and Licenses ─────────────────────────────────────────────────────

@pytest.fixture
def updater(monkeypatch, tmp_path):
    from app.api.v1 import system_updates

    monkeypatch.setattr(system_updates, "CONFIG_PATH", tmp_path / "agent.conf")
    monkeypatch.setattr(system_updates, "TIMER_DROPIN_FILE", str(tmp_path / "override.conf"))
    monkeypatch.setattr(system_updates, "TYPED_PRIVILEGE_BROKER", str(tmp_path / "no-broker"))
    calls = []
    monkeypatch.setattr(system_updates.subprocess, "run", lambda argv, **kw: calls.append(list(argv)) or SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    return system_updates, calls, tmp_path


def test_saving_update_settings_leaves_the_timer_alone_unless_the_interval_changes(db, updater):
    system_updates, calls, tmp_path = updater
    body = {"auto_update": False, "check_interval_hours": 4,
            "maintenance_window_start": "02:00", "maintenance_window_end": "03:00"}
    with admin(db) as c:
        # No drop-in: the shipped unit runs every 4 hours. Changing the window must not touch systemd,
        # because the helper restarts the timer and so starts one that was stopped on purpose.
        assert c.put("/api/v1/system/update-config", json=body).json()["status"] == "ok"
        assert calls == []
        assert "maintenance_window_start = 02:00" in (tmp_path / "agent.conf").read_text()

        assert c.put("/api/v1/system/update-config", json={**body, "check_interval_hours": 6}).json()["status"] == "ok"
        assert calls == [["sudo", "-n", system_updates.TIMER_HELPER, "set-interval", "6"]]

        (tmp_path / "override.conf").write_text("# Managed\n[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=6h\n")
        calls.clear()
        assert c.put("/api/v1/system/update-config", json={**body, "check_interval_hours": 6,
                                                           "auto_update": True}).json()["status"] == "ok"
        assert calls == []

        for bad in (0, -4, 169):
            assert c.put("/api/v1/system/update-config", json={**body, "check_interval_hours": bad}).status_code == 422


def test_portal_refusals_are_not_relayed_as_an_expired_session(updater):
    system_updates, _calls, tmp_path = updater
    assert system_updates._relay_status(401) == 502 and system_updates._relay_status(403) == 502
    assert system_updates._relay_status(503) == 502
    assert system_updates._relay_status(409) == 409 and system_updates._relay_status(422) == 422


def test_an_unreadable_cached_subscription_is_no_subscription_not_an_error(monkeypatch, tmp_path):
    from app.api.v1 import system_updates

    path = tmp_path / "subscription.json"
    monkeypatch.setattr(system_updates, "SUBSCRIPTION_PATH", path)
    path.write_text(json.dumps({"id": "s1", "max_devices": None, "used_slots": None}))
    assert system_updates._load_subscription() is None
    path.write_text(json.dumps({"id": "s1", "max_devices": 50}))
    assert system_updates._load_subscription().max_devices == 50


# ── MIB Library ──────────────────────────────────────────────────────────────

def test_the_mib_library_works_for_the_permission_its_tab_is_shown_under(db):
    with api_client(db, role="t_rev_settings_manager", permissions=["settings.manage"], username="rev-settings") as c:
        assert c.get("/api/v1/snmp/mibs").status_code == 200
        assert c.delete(f"/api/v1/snmp/mibs/{uuid.uuid4()}").status_code == 404      # allowed; nothing to delete
    with api_client(db, role="t_rev_device_viewer", permissions=["devices.view"], username="rev-viewer") as c:
        assert c.get("/api/v1/snmp/mibs").status_code == 200
        assert c.delete(f"/api/v1/snmp/mibs/{uuid.uuid4()}").status_code == 403
    with api_client(db, role="t_rev_alerts_only", permissions=["alerts.view"], username="rev-alerts") as c:
        assert c.get("/api/v1/snmp/mibs").status_code == 403
