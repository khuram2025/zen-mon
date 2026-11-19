"""Security and operations alerts (alerting plan G-16, migrate-206).

ZenPlus raised nothing on bursts of failed sign-ins, on administrators changing
accounts, roles, sign-in settings or alert rules, or on scheduled report runs
that failed or were only partly delivered. These are three more metrics of the
"appliance" rule family (appliance_alert_service).

Pinned here: the pure readings and wording (no typed non-account name and no
address ever reaches the text), the event-like burst lifecycle, the
migration's seeds, and each collector with the lifecycle end to end on a
disposable PostgreSQL with a recording dispatcher.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from app.services import alert_rule_scope as rs
from app.services import appliance_alert_service as apl

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)
from test_appliance_health_alerts import (  # noqa: F401  (world is a fixture used by name)
    T0, incidents, only, rule, rule_id, run, session, world)

MIGRATION = Path(__file__).parents[2] / "scripts" / "migrate-206-security-ops-alerts.sql"


# ══ Readings and wording (pure) ══════════════════════════════════════════════

def test_login_reasons_follow_the_audit_text():
    assert apl.login_reason("Invalid username or password") == "invalid"
    assert apl.login_reason("Too many failed sign-in attempts; the account is temporarily locked") == "locked"
    assert apl.login_reason("Invalid verification code — account locked") == "locked"
    assert apl.login_reason("Too many failed sign-in attempts. Try again in about 3 minute(s)") == "blocked"
    assert apl.login_reason("mfa") == "mfa"
    assert apl.login_reason("Account is disabled") == "disabled"
    assert apl.login_reason("This account signs in with single sign-on") == "sso_only"
    assert apl.login_reason("The identity provider reported: access_denied", "oidc") == "sso"


def test_login_text_names_accounts_but_never_typed_names_or_addresses():
    acct = apl.login_reading({"dim": "user:admin", "account": "Admin", "n": 12, "last_reason": "mfa",
                              "locked": True, "sources": 2, "names": 1})
    assert acct.label == "user Admin" and acct.value == 12
    s = apl.sentence(rule(apl.LOGIN, ">=", 10), acct)
    assert s == ("ZenPlus appliance: 12 failed sign-ins for user Admin from 2 addresses in the last"
                 " 15 minutes (limit 10; last reason: wrong MFA code); the account was locked")
    assert apl.sentence(rule(apl.LOGIN, ">=", 10), apl.Reading(3.0, dimension="user:admin",
                                                               label="user Admin"), recovery=True) == \
        "ZenPlus appliance: failed sign-ins for user Admin are below the limit again (3 in the last 15 minutes)"

    ip = apl.login_reading({"dim": "ip:10.9.9.9", "account": None, "n": 14, "names": 5, "sources": 1,
                            "last_reason": "Invalid username or password"})
    s = apl.sentence(rule(apl.LOGIN, ">=", 10), ip)
    assert s == ("ZenPlus appliance: 14 failed sign-ins for unknown accounts from one address"
                 " (5 names tried) in the last 15 minutes (limit 10; last reason: wrong username or password)")
    assert "10.9.9.9" not in s + ip.label
    # An e-mail style account name is cleaned like any alert text.
    mail = apl.login_reading({"dim": "user:j@corp.example", "account": "j@corp.example", "n": 10})
    assert "corp.example" not in mail.label


def test_admin_change_labels():
    L = apl.admin_change_label
    assert L("user.create", {"username": "jsmith"}) == "user jsmith created"
    assert L("user.update", {"username": "jsmith", "changes": {"role": {"from": "viewer", "to": "admin"}}}) \
        == "user jsmith role set to admin"
    assert L("user.update", json.dumps({"username": "jsmith", "changes": {"is_active": {"to": False}}})) \
        == "user jsmith disabled"
    assert L("role.update", {"name": "Operators"}) == "role Operators changed"
    assert L("settings.auth.update", {}, "auth.ldap") == "LDAP settings changed"
    assert L("settings.auth.toggle", {"enabled": False}, "auth.oidc") == "single sign-on sign-in turned off"
    assert L("alert_rule.toggle", {"name": "Device Down", "enabled": False}) == "alert rule Device Down turned off"
    assert L("notification_channel.delete", {}) == "notification channel deleted"
    assert L("apm.ingest_key.create", {"name": "web"}) == "APM ingest key web created"


def test_admin_reading_and_wording():
    rows = [{"at": T0 - 5, "action": "user.create", "actor": "admin", "metadata": {"username": "a"}},
            {"at": T0 - 1, "action": "role.delete", "actor": "ops", "metadata": {"name": "Guests"}},
            {"at": T0 - 3, "action": "user.delete", "actor": None, "metadata": {"username": "b"}},
            {"at": T0 - 4, "action": "security.tls.config", "actor": "admin", "metadata": {}}]
    r = apl.admin_reading(rows, None, T0)
    assert r.value == 4 and r.facts["last_event_at"] == T0 - 1
    assert r.facts["event_times"] == [T0 - 1, T0 - 3, T0 - 4, T0 - 5]
    assert apl.sentence(rule(apl.ADMIN, ">=", 1), r) == (
        "ZenPlus appliance: 4 administrative changes in the last 15 minutes: role Guests deleted by ops;"
        " user b deleted by the system; HTTPS settings changed by admin; and 1 more")
    assert apl.sentence(rule(apl.ADMIN, ">=", 1), apl.Reading(0.0, facts={"quiet_s": 900}), recovery=True) \
        == "ZenPlus appliance: no further administrative changes for 15 minutes"


def test_event_view_counts_only_unreported_events_and_clears_after_the_quiet_time():
    r = apl.admin_reading([{"at": T0 - 600, "action": "user.create", "actor": "a", "metadata": {}},
                           {"at": T0 - 60, "action": "user.delete", "actor": "a", "metadata": {}}], None, T0)
    adm = rule(apl.ADMIN, ">=", 1, clear_duration=None)
    view, breach, cleared = apl.event_view(adm, r, now=T0, covered=None)
    assert (view.value, breach, cleared) == (2.0, True, False)
    # An earlier incident reported events up to T0-600: only the newer one counts.
    view, breach, _ = apl.event_view(adm, r, now=T0, covered=T0 - 600)
    assert view.value == 1 and [e["what"] for e in view.facts["events"]] == ["user deleted"]
    view, breach, _ = apl.event_view(adm, r, now=T0, covered=T0 - 60)
    assert view.value == 0 and breach is False
    # Quiet for the clear hold (NULL = 900 s) since the newest change.
    assert apl.event_view(adm, r, now=T0 - 60 + 899, covered=None)[2] is False
    assert apl.event_view(adm, r, now=T0 - 60 + 900, covered=None)[2] is True
    assert apl.event_view(rule(apl.ADMIN, ">=", 1, clear_duration=120), r, now=T0 + 60, covered=None)[2]


def test_report_reading():
    sched = {"id": "s1", "name": "Weekly SLA"}
    assert apl.report_reading(sched, None).value is None
    failed = apl.report_reading(sched, {"status": "failed", "error": "NOC mail: 550 rejected ops@acme.example",
                                        "generated_at": T0})
    assert failed.value == 1 and failed.dimension == "s1" and "acme" not in failed.facts["error"]
    partial = apl.report_reading(sched, {"status": "partial", "error": None,
                                         "notes": json.dumps(["Retry 1: nothing delivered",
                                                              "Section Availability failed"])})
    assert partial.value == 1 and partial.facts["error"] == "Section Availability failed"
    assert apl.report_reading(sched, {"status": "success"}).value == 0
    assert apl.report_reading(sched, {"status": "skipped"}).value == 0
    s = apl.sentence(rule(apl.REPORT), partial)
    assert s == ('ZenPlus appliance: report schedule "Weekly SLA" was only partly delivered'
                 ' (error: Section Availability failed)')
    assert apl.sentence(rule(apl.REPORT), apl.Reading(1.0, label='report schedule "Weekly SLA"',
                                                       facts={"status": "failed"})) == \
        'ZenPlus appliance: report schedule "Weekly SLA" failed: nothing was delivered'
    assert apl.sentence(rule(apl.REPORT), apl.Reading(0.0, label='report schedule "Weekly SLA"'),
                        recovery=True) == 'ZenPlus appliance: report schedule "Weekly SLA" was delivered again'


def test_scope_labels_and_preview():
    assert rs.family({"metric": apl.LOGIN}) == "appliance"
    assert rs.describe_scope({"metric": apl.LOGIN})["label"] == "Every account and source address"
    assert rs.describe_scope({"metric": apl.REPORT})["label"] == "Every report schedule"
    assert rs.describe_scope({"metric": apl.ADMIN})["label"] == "ZenPlus appliance"
    for metric in (apl.LOGIN, apl.ADMIN, apl.REPORT):
        for recovery in (False, True):
            v = apl.preview_variables({"metric": metric, "operator": ">=", "threshold": 1, "name": "x"},
                                      is_recovery=recovery)
            assert v["hostname"] == "ZenPlus appliance" and v["event_sentence"].startswith("ZenPlus appliance: ")


# ══ Migration ════════════════════════════════════════════════════════════════

SEEDED = {
    # name: (metric, operator, threshold, severity, priority, min_duration, clear_duration, enabled)
    "Repeated failed sign-ins": ("appliance_login_failures", ">=", 10, "warning", "P3", 0, 900, True),
    "Administrative changes": ("appliance_admin_changes", ">=", 1, "info", "P4", 0, 900, False),
    "Scheduled report failed": ("appliance_report_failed", ">", 0, "warning", "P3", 0, 0, True),
}


def test_migration_seeds_the_rules_once_and_widens_the_check(pg_db):
    def rows():
        return {r[0]: r[1:] for r in pg_db.query(
            "SELECT name, metric, operator, threshold, severity, priority, min_duration, clear_duration,"
            " enabled FROM alert_rules WHERE metric = ANY($1::text[])", [v[0] for v in SEEDED.values()])}
    assert rows() == SEEDED
    pg_db.psql_file(MIGRATION)
    assert rows() == SEEDED
    check = pg_db.scalar("SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                         " WHERE conname = 'alert_rules_metric_check'")
    assert "appliance_disk_pct" in check and "appliance_report_failed" in check
    with pytest.raises(Exception):
        pg_db.execute("INSERT INTO alert_rules (name, metric, operator, threshold, severity)"
                      " VALUES ('x', 'appliance_bogus', '>', 0, 'warning')")


# ══ Collectors and lifecycle on a disposable PostgreSQL ══════════════════════

def audit(db, action, at, *, actor=None, metadata=None, resource_id=None):
    db.execute("INSERT INTO audit_logs (actor_username, action, resource_type, resource_id, metadata,"
               " created_at) VALUES ($1, $2, 'x', $3, $4::jsonb, to_timestamp($5))",
               actor, action, resource_id, metadata or {}, at)


def failed_login(db, at, username, ip, reason="Invalid username or password", **extra):
    meta = {"source_ip": ip, "reason": reason, **extra}
    if username is not None:
        meta["username"] = username
    audit(db, "auth.login_failed", at, metadata=meta)


@pytest.mark.asyncio
async def test_failed_sign_ins_per_account_or_address_with_hold_and_clear(world):
    w = world
    only(w, "Repeated failed sign-ins")
    w.db.execute("INSERT INTO users (username, email, password_hash) VALUES ('JSmith', 'j@x.example', 'x')")
    for i in range(10):          # an account, typed in another case, locked on the last try
        failed_login(w.db, T0 - 120 + i, "JSMITH", "192.0.2.7",
                     "Too many failed sign-in attempts; the account is temporarily locked" if i == 9 else
                     "Invalid username or password")
    for i in range(12):          # names that are no account (one a mistyped password), one address
        failed_login(w.db, T0 - 100 + i, f"hunter2-secret-{i % 4}", "198.51.100.9")
    for i in range(9):           # single sign-on failures (no name), below the limit
        failed_login(w.db, T0 - 90 + i, None, "203.0.113.5", "The identity provider reported: x",
                     method="oidc")
    failed_login(w.db, T0 - 2000, "JSmith", "192.0.2.7")          # outside the window

    await run(w, T0)
    incs = incidents(w)
    assert sorted(i.meta["dimension"] for i in incs) == ["ip:198.51.100.9", "user:jsmith"]
    by_dim = {i.meta["dimension"]: i for i in incs}
    acct = by_dim["user:jsmith"]
    assert (acct.status, acct.severity, acct.priority) == ("active", "warning", "P3")
    assert acct.message == ("ZenPlus appliance: 10 failed sign-ins for user JSmith in the last 15 minutes"
                            " (limit 10; last reason: account locked by the failure threshold);"
                            " the account was locked")
    other = by_dim["ip:198.51.100.9"]
    assert other.message.startswith("ZenPlus appliance: 12 failed sign-ins for unknown accounts from one"
                                    " address (4 names tried)")
    text_out = json.dumps([[i.message for i in incs], [ctx for _, ctx in w.sent]], default=str)
    for secret in ("hunter2", "198.51.100.9", "192.0.2.7"):
        assert secret not in text_out
    assert len(w.sent) == 2 and {c["priority"] for _, c in w.sent} == {"P3"}
    assert sorted(dict(c["details"])["Sign-ins"] for _, c in w.sent) == \
        ["unknown accounts from one address", "user JSmith"]
    # The 9 SSO failures were tracked but stay below the limit.
    assert w.db.scalar("SELECT last_value FROM appliance_alert_state WHERE dimension = 'ip:203.0.113.5'") == 9

    # The failures leave the 15-minute window at about T0+780..T0+890; the
    # open incidents keep being read (0) and clear 900 s later.
    for t in range(60, 1680, 60):
        await run(w, T0 + t)
    assert {i.status for i in incidents(w)} == {"active"} and len(w.sent) == 2
    assert w.db.scalar("SELECT count(*) FROM appliance_alert_state WHERE dimension = 'ip:203.0.113.5'") == 0
    for t in range(1680, 1860, 60):
        await run(w, T0 + t)
    assert {i.status for i in incidents(w)} == {"resolved"}
    recoveries = [c for _, c in w.sent if c["is_recovery"]]
    assert len(recoveries) == 2
    assert any("failed sign-ins for user JSmith are below the limit again (0 in the last 15 minutes)"
               in c["body"] for c in recoveries)


@pytest.mark.asyncio
async def test_admin_changes_page_once_per_burst(world):
    w = world
    only(w, "Administrative changes")
    rid = rule_id(w, "Administrative changes")
    w.db.execute("UPDATE alert_rules SET enabled = TRUE, clear_duration = 300 WHERE id = $1", rid)
    # Not counted: turning a rule on, a user's own time zone, a sign-in.
    audit(w.db, "alert_rule.toggle", T0 - 30, actor="admin", metadata={"name": "CPU", "enabled": True})
    audit(w.db, "user.update", T0 - 30, actor="admin",
          metadata={"username": "jsmith", "changes": {"timezone": {"to": "UTC"}}})
    audit(w.db, "auth.login_succeeded", T0 - 30, metadata={"username": "admin"})
    await run(w, T0)
    assert incidents(w) == []

    audit(w.db, "user.update", T0 + 10, actor="admin",
          metadata={"username": "jsmith", "changes": {"role": {"from": "viewer", "to": "admin"}}})
    await run(w, T0 + 60)
    [inc] = incidents(w)
    assert (inc.severity, inc.priority) == ("info", "P4")
    assert inc.message == ("ZenPlus appliance: 1 administrative change in the last 15 minutes:"
                           " user jsmith role set to admin by admin")
    [(_, ctx)] = w.sent
    assert ctx["priority"] == "P4" and ctx["groupable"] is True and ctx["delivery_event"] == "trigger"

    # More changes in the same burst: the same incident, nothing re-sent.
    audit(w.db, "alert_rule.toggle", T0 + 100, actor="ops", metadata={"name": "Device Down", "enabled": False})
    await run(w, T0 + 120)
    [inc] = incidents(w)
    assert inc.meta["value"] == 2 and len(w.sent) == 1
    # Quiet for the 300 s clear hold since T0+100: resolved with a notice.
    for t in range(180, 400, 60):
        await run(w, T0 + t)
    assert incidents(w)[0].status == "active"
    await run(w, T0 + 420)
    assert incidents(w)[0].status == "resolved"
    assert w.sent[-1][1]["is_recovery"] and "no further administrative changes for 5 minutes" in w.sent[-1][1]["body"]
    # The reported changes are still inside the 15-minute window: no new incident.
    for t in range(480, 900, 60):
        await run(w, T0 + t)
    assert len(incidents(w)) == 1
    # A new change is a new burst.
    audit(w.db, "role.delete", T0 + 910, actor="admin", metadata={"name": "Guests"})
    await run(w, T0 + 960)
    assert [i.status for i in incidents(w)] == ["resolved", "active"]
    assert incidents(w)[1].message.endswith(": role Guests deleted by admin")


@pytest.mark.asyncio
async def test_admin_changes_rule_is_seeded_off(world):
    w = world
    audit(w.db, "user.delete", T0 - 10, actor="admin", metadata={"username": "x"})
    await run(w, T0)
    assert [i for i in incidents(w) if i.rule == "Administrative changes"] == []


def schedule(db, name, enabled=True):
    sid = uuid.uuid4()
    db.execute("INSERT INTO report_schedules (id, name, enabled, report_type, period, format, frequency,"
               " hour, minute) VALUES ($1, $2, $3, 'availability', 'last_7d', 'pdf', 'daily', 8, 0)",
               sid, name, enabled)
    return sid


def report_run(db, sid, status, at, *, error=None, retry_at=None):
    db.execute("INSERT INTO report_runs (schedule_id, report_type, period, title, token, html, status, error,"
               " generated_at, next_retry_at) VALUES ($1, 'availability', 'last_7d', 't', $2, '', $3, $4,"
               " to_timestamp($5), to_timestamp($6))",
               sid, uuid.uuid4().hex, status, error, at, retry_at)


@pytest.mark.asyncio
async def test_report_failures_page_per_schedule_and_clear_on_the_next_good_run(world):
    w = world
    only(w, "Scheduled report failed")
    a, b, c = schedule(w.db, "Weekly SLA"), schedule(w.db, "Daily NOC"), schedule(w.db, "Old", enabled=False)
    report_run(w.db, a, "success", T0 - 86400 * 2)
    report_run(w.db, a, "failed", T0 - 600, error="NOC mail: 550 5.1.1 ops@acme.example unknown")
    report_run(w.db, b, "success", T0 - 86400)
    report_run(w.db, b, "failed", T0 - 300, error="451 try later", retry_at=T0 + 600)   # retry pending
    report_run(w.db, c, "failed", T0 - 600)                                              # turned off
    await run(w, T0)
    [inc] = incidents(w)
    assert inc.meta["dimension"] == str(a) and (inc.severity, inc.priority) == ("warning", "P3")
    assert inc.message.startswith('ZenPlus appliance: report schedule "Weekly SLA" failed: nothing was delivered'
                                  ' (run at ')
    assert inc.message.endswith("(error: NOC mail: 550 5.1.1 <address> unknown)")
    [(_, ctx)] = w.sent
    assert ("Report schedule", 'report schedule "Weekly SLA"') in ctx["details"]

    # The retry gave up and left the other run partial: now it alerts too.
    w.db.execute("UPDATE report_runs SET next_retry_at = NULL, status = 'partial' WHERE schedule_id = $1", b)
    await run(w, T0 + 60)
    assert sorted(i.meta["dimension"] for i in incidents(w)) == sorted([str(a), str(b)])
    assert any("was only partly delivered" in i.message for i in incidents(w))

    # The next run of "Weekly SLA" succeeds: cleared at once with a notice.
    report_run(w.db, a, "running", T0 + 100)
    await run(w, T0 + 120)
    assert all(i.status == "active" for i in incidents(w))
    w.db.execute("UPDATE report_runs SET status = 'success' WHERE schedule_id = $1 AND status = 'running'", a)
    await run(w, T0 + 180)
    by_dim = {i.meta["dimension"]: i for i in incidents(w)}
    assert by_dim[str(a)].status == "resolved" and by_dim[str(b)].status == "active"
    assert w.sent[-1][1]["body"].startswith('ZenPlus appliance: report schedule "Weekly SLA" was delivered again')

    # "Daily NOC" is deleted: closed without a notice.
    sent = len(w.sent)
    w.db.execute("DELETE FROM report_schedules WHERE id = $1", b)
    await run(w, T0 + 240)
    closed = {i.meta["dimension"]: i for i in incidents(w)}[str(b)]
    assert closed.status == "resolved"
    assert closed.meta["resolved_reason"] == "the report schedule was deleted or turned off"
    assert len(w.sent) == sent


@pytest.mark.asyncio
async def test_rule_api_accepts_the_new_metrics(world, monkeypatch):
    from app.api.v1 import alert_rules

    async def allow(*a, **k):
        return None

    monkeypatch.setattr(rs, "guard_rule_change", allow)
    monkeypatch.setattr(alert_rules, "write_audit_log", allow)
    user = NS(id=None, role="admin", scope_tags=[])
    async with session(world.db) as s:
        for metric in (apl.LOGIN, apl.ADMIN, apl.REPORT):
            created = await alert_rules.create_alert_rule(alert_rules.AlertRuleCreate(
                name=f"x {metric}", metric=metric, operator=">=", threshold=5, scope_tag="lab"), s, user)
            assert created["family"] == "appliance" and created["scope_tag"] is None
        report_rule = {"metric": apl.REPORT}
        assert (await rs.affected(s, report_rule))["noun"] == "report schedules"
