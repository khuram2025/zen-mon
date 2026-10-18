"""Platform section pack (report_packs/platform): sensors, access and audit,
license, retention, storage, backups and report delivery — KPIs, rows and
formatting from canned rows, the organisation timezone, option effects and
the guarantee that secrets and audit metadata values are never selected."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.services import report_sections as rs
from app.services import report_templates as rt
from app.services.branding import Branding
from app.services.report_packs import platform as pack

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)      # Wednesday; 15:00 in Asia/Riyadh
TO = NOW.replace(tzinfo=None)
FRM = TO - timedelta(days=7)
TS = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)         # "Oct 07 12:00" in Asia/Riyadh


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
    """Answers each query with the first route whose SQL substring matches (a
    route may be a callable of (sql, params)); records every statement."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        params = dict(params or {})
        self.calls.append((sql, params))
        for key, res in self.routes.items():
            if key in sql:
                return res(sql, params) if callable(res) else res
        return Result()

    def begin_nested(self):
        from tests.conftest import _NullTransaction
        return _NullTransaction()

    async def rollback(self):
        return None

    def sql(self) -> str:
        return "\n".join(s for s, _p in self.calls)

    def params_for(self, fragment: str) -> dict:
        return next(p for s, p in self.calls if fragment in s)


class _CHResult:
    def __init__(self, rows):
        self.result_rows = rows


class FakeCH:
    def __init__(self, routes: dict, fail: bool = False):
        self.routes = routes
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None, **kw):
        self.calls.append((sql, dict(parameters or {})))
        if self.fail:
            raise ConnectionError("clickhouse down")
        for key, rows in self.routes.items():
            if key in sql:
                return _CHResult(list(rows))
        return _CHResult([])


@pytest.fixture(autouse=True)
def _pinned_now(monkeypatch):
    monkeypatch.setattr(pack, "_now", lambda: NOW)
    monkeypatch.delenv("ZENPLUS_MIN_SENSOR_VERSION", raising=False)


def run(db, sid, options=None, ch=None, monkeypatch=None, frm=FRM, to=TO):
    """Run one section function directly (exceptions surface in the test)."""
    if ch is not None:
        monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: ch)
    ctx = rs.SectionCtx(db, frm, to, None)
    ctx.branding = Branding(timezone="Asia/Riyadh")
    ctx.section_id = sid
    ctx.section_options = rs.normalize_options(sid, options or {})
    sec = asyncio.run(rs.SECTION_REGISTRY[sid]["fn"](ctx))
    well_formed(sec)
    return sec


def kpi(sec, label):
    return next(k for k in sec["kpis"] if k["label"] == label)


def table(sec, prefix):
    return next(t for t in sec["tables"] if (t.get("title") or "").startswith(prefix))


def col(t, header):
    return t["headers"].index(header)


def well_formed(sec):
    for t in sec["tables"]:
        assert len(t["styles"]) == len(t["headers"])
        for row in t["rows"]:
            assert len(row) == len(t["headers"]), row
            assert all(isinstance(c, str) for c in row), row
    for k in sec["kpis"]:
        assert isinstance(k["value"], str)
    assert 0 < len(sec["kpis"]) <= 6 or not sec["kpis"]


ROLES = Result([
    ("admin", "Administrator", ["system.admin"], [], True),
    ("operator", "Operator", ["devices.view", "devices.manage", "alerts.view"], [], True),
    ("viewer", "Viewer", ["devices.view"], ["branch"], True),
])


# ── templates and registration ──────────────────────────────────────────────

PACK_SECTIONS = ("sensor_fleet_health", "sensor_availability", "sensor_results_volume", "access_review",
                 "login_activity", "audit_activity", "role_changes", "active_sessions", "license_usage",
                 "data_retention_coverage", "storage_utilization", "backup_status", "report_delivery")
TEMPLATES = ("user_access_review", "sign_in_activity", "administrative_audit_trail",
             "role_permission_changes", "active_sessions_report", "sensor_fleet_health_report",
             "sensor_availability_report", "license_usage_report", "data_retention_coverage_report",
             "storage_backup_status", "report_delivery_health")


def test_sections_and_templates_registered():
    for sid in PACK_SECTIONS:
        e = rs.SECTION_REGISTRY[sid]
        assert e["scope"] == "fleet" and e["fn"].__module__ == pack.__name__
    assert rs.SECTION_REGISTRY["login_activity"]["requires"] == ("audit.view",)
    assert rs.SECTION_REGISTRY["audit_activity"]["requires"] == ("audit.view",)
    assert rs.SECTION_REGISTRY["data_retention_coverage"]["requires"] == ("system.admin",)
    assert rs.SECTION_REGISTRY["report_delivery"]["requires"] == ("reports.view",)
    for sid in ("sensor_fleet_health", "access_review", "active_sessions", "license_usage",
                "data_retention_coverage"):
        assert rs.SECTION_REGISTRY[sid]["semantics"] == "snapshot"
    assert rt.validate_catalog() == []
    for key in TEMPLATES:
        t = rt.TEMPLATES[key]
        assert t.category in ("security", "platform")
        assert key in rs.REPORT_PRESETS
    assert rt.TEMPLATES["user_access_review"].featured
    # A user administrator without audit.view sees the access review, not sign-ins.
    perms = frozenset({"users.view", "roles.view"})
    assert rs.may_view_section(perms, "access_review")
    assert not rs.may_view_section(perms, "login_activity")


# ── sensor_fleet_health ─────────────────────────────────────────────────────

def _sensor(sid, name, status, hb_age_s, version, minimum=None, dropped=0, devices=0, checks=0,
            reason=None, site="Riyadh DC"):
    hb = NOW - timedelta(seconds=hb_age_s) if hb_age_s is not None else None
    return (sid, name, site, status, reason, version, minimum, hb, True, False, 90, 180, 2, dropped,
            86400 * 3, devices, checks)


def test_sensor_fleet_health_live_state_versions_and_targets():
    db = FakeDB({"FROM sensors s LEFT JOIN sites": Result([
        _sensor("s1", "edge-a", "online", 10, "1.24.0", "1.23.5", devices=40, checks=5),
        _sensor("s2", "edge-b", "online", 300, "1.23.4", "1.23.5", dropped=12, devices=10,
                reason="Heartbeat overdue beyond offline threshold"),
        _sensor("s3", "edge-c", "disabled", None, "1.24.0", "1.23.5"),
    ])})
    sec = run(db, "sensor_fleet_health")
    assert kpi(sec, "Sensors")["value"] == "3"
    assert kpi(sec, "Online")["value"] == "1" and kpi(sec, "Online")["subtitle"] == "of 2 enabled"
    off = kpi(sec, "Offline or degraded")
    assert off["value"] == "1" and off["accent"] == "danger"     # stored 'online', heartbeat 5 min old
    assert kpi(sec, "Below minimum version")["value"] == "1"
    assert kpi(sec, "Results dropped")["value"] == "12"
    assert kpi(sec, "Assigned targets")["value"] == "55"
    t = table(sec, "Sensors")
    first = t["rows"][0]
    assert first[0] == "edge-b" and first[col(t, "State")] == "offline"
    assert first[col(t, "Last heartbeat")] == "5 min ago"
    assert first[col(t, "Version")] == "1.23.4 (below 1.23.5)"
    assert t["rows"][-1][col(t, "State")] == "disabled"
    att = table(sec, "Sensors needing attention")
    assert att["rows"][0][2] == "Heartbeat overdue beyond offline threshold"
    # The API key hash itself is never selected, only whether one exists.
    assert "api_key_hash IS NOT NULL" in db.sql() and "s.api_key_hash," not in db.sql()


def test_sensor_fleet_health_top_n_and_empty():
    db = FakeDB({"FROM sensors s LEFT JOIN sites": Result([
        _sensor(f"s{i}", f"edge-{i}", "online", 5, "1.24.0") for i in range(3)])})
    sec = run(db, "sensor_fleet_health", {"top_n": 1})
    t = table(sec, "Sensors")
    assert t["title"] == "Sensors — top 1 of 3 sensors" and len(t["rows"]) == 1
    assert kpi(sec, "Below minimum version")["subtitle"] == "No minimum version configured"
    empty = run(FakeDB({}), "sensor_fleet_health")
    assert empty["notes"] and "No remote sensors" in empty["notes"][0]


# ── sensor_availability ─────────────────────────────────────────────────────

def _avail_db(created=None):
    t1 = NOW - timedelta(days=6)
    return FakeDB({
        "FROM sensors s LEFT JOIN sites": Result([
            ("s1", "edge-a", "Riyadh DC", "online", created or NOW - timedelta(days=60), NOW, True, False,
             90, 180),
            ("s2", "edge-b", "Jeddah", "online", NOW - timedelta(days=60), NOW, True, False, 90, 180),
        ]),
        "AND ts < :f ORDER BY": Result([("s1", "online")]),
        "ts >= :f AND ts < :t": Result([
            ("s1", t1, "online", "offline"),
            ("s1", t1 + timedelta(hours=2), "offline", "online"),
        ]),
        "ts >= :t ORDER BY": Result([]),
    })


def test_sensor_availability_rebuilds_intervals_with_two_decimals():
    db = _avail_db()
    sec = run(db, "sensor_availability")
    # edge-a: 2 h offline of 168 h -> 98.81 %; edge-b: no change recorded -> current state.
    t = table(sec, "Availability by sensor")
    assert t["rows"][0][0] == "edge-a"
    assert t["rows"][0][col(t, "Availability")] == "98.81%"
    assert t["rows"][0][col(t, "Offline")] == "2 h 00 min"
    assert t["rows"][0][col(t, "Episodes")] == "1" and t["rows"][0][col(t, "Now")] == "online"
    assert t["rows"][1][col(t, "Availability")] == "100.00%"
    assert kpi(sec, "Fleet sensor availability")["value"] == "99.40%"
    assert kpi(sec, "Sensors below target")["value"] == "1"
    assert kpi(sec, "Longest offline")["subtitle"] == "edge-a"
    assert kpi(sec, "Status changes")["value"] == "2"
    assert any("starting state" in n and "1 sensor(s)" in n for n in sec["notes"])
    rank = next(c for c in sec["charts"] if c["series"]["kind"] == "rank")
    assert [p["label"] for p in rank["series"]["points"]] == ["edge-a", "edge-b"]     # lowest first
    # Daily chart: local (Asia/Riyadh) midnights, two decimals.
    day = next(c for c in sec["charts"] if c["title"].startswith("Fleet sensor availability"))
    assert day["series"]["points"][0]["t"] == "2026-09-29T21:00:00Z"
    assert all(p["v"] is None or round(p["v"], 2) == p["v"] for p in day["series"]["points"])


def test_sensor_availability_target_option_and_retention_note():
    sec = run(_avail_db(), "sensor_availability", {"target": 98.5})
    assert kpi(sec, "Sensors below target")["value"] == "0"
    old = run(_avail_db(), "sensor_availability", frm=TO - timedelta(days=60))
    assert any("kept for 30 days" in n for n in old["notes"])
    gone = run(_avail_db(), "sensor_availability", frm=TO - timedelta(days=90), to=TO - timedelta(days=40))
    assert not gone["tables"] and "ended before" in gone["notes"][-1]


# ── sensor_results_volume ───────────────────────────────────────────────────

DAY1 = int(datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc).timestamp())   # Oct 06 00:00 Riyadh


def _volume_ch():
    return FakeCH({
        "zenplus.ping_metrics": [("central", DAY1, 1000, TS), ("s1", DAY1, 500, TS)],
        "zenplus.snmp_metrics": [("s1", DAY1, 300, TS)],
        "zenplus.service_metrics": [("0f1e2d3c-old-poller", DAY1, 50, TS)],
    })


def _volume_db():
    return FakeDB({"FROM sensors s LEFT JOIN sites": Result([
        ("s1", "edge-a", "Riyadh DC", "online"), ("s2", "edge-b", "Jeddah", "online"),
        ("s3", "edge-new", "", "pending")])})


def test_sensor_results_volume_per_poller(monkeypatch):
    ch = _volume_ch()
    sec = run(_volume_db(), "sensor_results_volume", ch=ch, monkeypatch=monkeypatch)
    assert kpi(sec, "Results stored")["value"] == "1,850"
    assert kpi(sec, "Pollers reporting")["value"] == "3"
    assert kpi(sec, "Silent sensors")["value"] == "1"           # edge-b; pending edge-new is not silent
    t = table(sec, "Results by poller")
    assert t["rows"][0][0] == pack.CONTROLLER_LABEL and t["rows"][0][col(t, "Ping")] == "1,000"
    edge = next(r for r in t["rows"] if r[0] == "edge-a")
    assert edge[col(t, "SNMP")] == "300" and edge[col(t, "Total")] == "800"
    assert edge[col(t, "Last result")] == "Oct 07 12:00"
    assert any(r[0].startswith("Unregistered poller") for r in t["rows"])
    assert t["rows"][-1][0] == "edge-b" and t["rows"][-1][col(t, "Last result")] == "None in period"
    q = next(p for s, p in ch.calls if "zenplus.ping_metrics" in s)
    assert q["org_tz"] == "Asia/Riyadh" and q["controller"]
    assert "INTERVAL 1 DAY" in next(s for s, _p in ch.calls if "zenplus.ping_metrics" in s)
    stacked = sec["charts"][0]["series"]
    assert stacked["kind"] == "stacked" and {s["name"] for s in stacked["series"]} >= {"edge-a"}


def test_sensor_results_volume_retention_window(monkeypatch):
    # 60-day window, 30 days of raw results: averages use the 30 days still held.
    sec = run(_volume_db(), "sensor_results_volume", ch=_volume_ch(), monkeypatch=monkeypatch,
              frm=TO - timedelta(days=60))
    assert kpi(sec, "Results stored")["subtitle"] == "62 per day"
    assert any("kept for 30 days" in n for n in sec["notes"])
    gone = run(_volume_db(), "sensor_results_volume", ch=_volume_ch(), monkeypatch=monkeypatch,
               frm=TO - timedelta(days=90), to=TO - timedelta(days=45))
    assert not gone["kpis"] and "ended before" in gone["notes"][-1]


def test_sensor_results_volume_kind_option(monkeypatch):
    ch = _volume_ch()
    sec = run(_volume_db(), "sensor_results_volume", {"kind": "snmp"}, ch=ch, monkeypatch=monkeypatch)
    assert sec["title"] == "Sensor Results Volume — SNMP"
    assert not any("zenplus.ping_metrics" in s for s, _p in ch.calls)
    t = table(sec, "Results by poller")
    assert "Total" not in t["headers"] and kpi(sec, "Results stored")["value"] == "300"


# ── access_review ───────────────────────────────────────────────────────────

def _users_db():
    return FakeDB({
        "FROM roles": ROLES,
        "FROM users u ORDER BY": Result([
            ("alice", "Alice A", "admin", True, "local", True, False, None, NOW - timedelta(days=1),
             NOW - timedelta(days=400), []),
            ("bob", "Bob B", "admin", True, "local", False, False, None, NOW - timedelta(days=200),
             NOW - timedelta(days=400), []),
            ("carol", "", "viewer", True, "ldap", False, False, None, NOW - timedelta(days=3),
             NOW - timedelta(days=100), ["riyadh"]),
            ("dave", "Dave D", "viewer", True, "local", False, True, NOW + timedelta(minutes=10),
             NOW - timedelta(days=2), NOW - timedelta(days=50), []),
            ("eve", "Eve E", "viewer", False, "local", False, False, None, None,
             NOW - timedelta(days=500), []),
        ]),
    })


def test_access_review_kpis_findings_and_no_secrets():
    db = _users_db()
    sec = run(db, "access_review")
    assert kpi(sec, "Active accounts")["value"] == "4"
    assert kpi(sec, "Active accounts")["subtitle"] == "1 disabled · 5 total"
    mfa = kpi(sec, "MFA coverage")
    assert mfa["value"] == "33.3%" and mfa["accent"] == "danger"
    assert mfa["subtitle"] == "1 of 3 active local accounts"
    priv = kpi(sec, "Privileged accounts")
    assert priv["value"] == "2" and priv["accent"] == "danger"
    assert priv["subtitle"] == "2 full administrators · 1 without MFA"
    assert kpi(sec, "Dormant (90+ days)")["value"] == "1"
    assert kpi(sec, "Locked now")["value"] == "1"
    assert kpi(sec, "Must change password")["value"] == "1"
    review = table(sec, "Accounts needing review")
    assert review["rows"][0][0] == "bob"
    assert review["rows"][0][col(review, "Findings")] == "Privileged without MFA; No sign-in for 200 days"
    dave = next(r for r in review["rows"] if r[0] == "dave")
    assert dave[col(review, "Findings")] == "Locked out; Must change password"
    roles = table(sec, "Accounts by role")
    admin = next(r for r in roles["rows"] if r[0] == "Administrator")
    assert admin[col(roles, "Privileged permissions")] == "Full administration"
    viewer = next(r for r in roles["rows"] if r[0] == "Viewer")
    assert viewer[col(roles, "Sign-in sources")] == "ldap 1, local 1"
    assert viewer[col(roles, "Role visibility scope")] == "branch"
    sql = db.sql()
    assert "password_hash" not in sql and "mfa_secret" not in sql and "recovery" not in sql


def test_access_review_dormant_days_option():
    sec = run(_users_db(), "access_review", {"days": 300})
    assert kpi(sec, "Dormant (300+ days)")["value"] == "0"
    review = table(sec, "Accounts needing review")
    bob = next(r for r in review["rows"] if r[0] == "bob")
    assert bob[col(review, "Findings")] == "Privileged without MFA"


# ── login_activity ──────────────────────────────────────────────────────────

def _login_db():
    day = int(datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc).timestamp())   # Oct 07 Riyadh

    def totals(sql, params):
        if params["f"] < datetime(2026, 9, 30, tzinfo=timezone.utc):
            return Result([(40, 2, 6, 1, 20, 3, 0, 0, 0)])       # previous period
        return Result([(50, 30, 8, 4, 25, 5, 2, 6, 9)])

    return FakeDB({
        # Most specific first: the per-username query also counts source addresses.
        "u.username = g.uname": Result([("admin", 12, 3, 4, 2, TS, True, 5),
                                        ("Winter2026!", 9, 0, 1, 0, TS, False, 5)]),
        "count(DISTINCT metadata->>'source_ip') FILTER": totals,
        "AS reason": Result([("invalid", 20, 7), ("locked", 2, 1), ("blocked", 6, 1), ("mfa", 2, 1)]),
        "AS ip": Result([("203.0.113.9", 18, 0, 6, TS, 2)]),
        "AS b,": Result([(day, 50, 30)]),
    })


def test_login_activity_kpis_reasons_and_timezone():
    db = _login_db()
    sec = run(db, "login_activity")
    ok = kpi(sec, "Successful sign-ins")
    assert ok["value"] == "50" and ok["subtitle"] == "▲ 10 vs previous period"
    failed = kpi(sec, "Failed sign-ins")
    assert failed["value"] == "30"
    assert failed["subtitle"] == "37.5% of attempts · ▲ 28 vs previous period"
    assert kpi(sec, "Account lockouts")["value"] == "2"
    assert kpi(sec, "Account lockouts")["subtitle"] == "6 attempt(s) while locked"
    assert kpi(sec, "Unknown-username failures")["value"] == "9"
    assert kpi(sec, "Signed-in accounts")["subtitle"] == "50.0% with MFA · 5 via SSO"
    reasons = table(sec, "Failed sign-ins by reason")
    assert reasons["rows"][0][:3] == ["Wrong username or password", "20", "66.7%"]
    names = table(sec, "Usernames with the most failed sign-ins")
    assert names["title"].endswith("top 2 of 5 usernames")
    assert names["rows"][1][col(names, "Account")] == "No such account"
    assert names["rows"][0][col(names, "Last failure")] == "Oct 07 12:00"
    ips = table(sec, "Source addresses")
    assert ips["rows"][0][0] == "203.0.113.9"
    p = db.params_for("AS b,")
    assert p["unit"] == "day" and p["org_tz"] == "Asia/Riyadh"
    chart = sec["charts"][0]["series"]
    assert [(s["name"], s["color"]) for s in chart["series"]] == [("Succeeded", "success"), ("Failed", "danger")]
    assert any(pt["t"] == "2026-10-06T21:00:00Z" and pt["v"] == 30 for pt in chart["series"][1]["points"])
    # Like patterns are bound, never interpolated.
    assert db.params_for("AS reason")["p_lock"] == "%temporarily locked%"


def test_login_activity_top_n_and_empty():
    db = _login_db()
    run(db, "login_activity", {"top_n": 3})
    assert db.params_for("u.username = g.uname")["lim"] == 3
    empty = run(FakeDB({}), "login_activity")
    assert empty["notes"] == ["No sign-in attempts were recorded in this period."]


# ── audit_activity ──────────────────────────────────────────────────────────

def _audit_db():
    def totals(sql, params):
        if params["f"] < datetime(2026, 9, 30, tzinfo=timezone.utc):
            return Result([(30, 2, 5, 1, 9)])
        return Result([(42, 3, 7, 4, 12)])

    return FakeDB({
        "count(DISTINCT action) FROM": totals,
        "GROUP BY action ORDER BY": Result([("service_check.update", 20, 2, TS)]),
        "GROUP BY resource_type": Result([("service_check", 20), ("user", 7), ("settings", 15)]),
        "max(actor_role)": Result([("admin", "admin", 30, 8, 5, TS), ("", None, 2, 1, 0, TS)]),
        "AS b,": Result([]),
        "jsonb_object_keys": Result([(TS, "admin", "admin", "settings.gateway.smtp.update", "settings",
                                      "smtp", ["host", "password"])]),
    })


def test_audit_activity_keys_only_and_kpis():
    db = _audit_db()
    sec = run(db, "audit_activity")
    assert sec["title"] == "Administrative Audit Trail"
    assert kpi(sec, "Audited events")["value"] == "42"
    assert kpi(sec, "Audited events")["subtitle"] == "▲ 12 vs previous period"
    assert kpi(sec, "Access-control events")["value"] == "7"
    assert kpi(sec, "Deletions")["value"] == "4"
    log = table(sec, "Latest events")
    row = log["rows"][0]
    assert row[col(log, "Fields recorded")] == "host, password"
    assert row[col(log, "Time")] == "2026-10-07 12:00"
    actors = table(sec, "Most active people")
    assert actors["rows"][1][0] == "system"
    log_sql = next(s for s, _p in db.calls if "jsonb_object_keys" in s)
    assert "metadata->" not in log_sql and "SELECT metadata" not in log_sql
    p = db.params_for("jsonb_object_keys")
    assert p["signin"] == ["auth.login_succeeded", "auth.login_failed", "auth.logout"]
    # Access-control counts never include sign-ins, even when the trail does.
    totals_sql = next(s for s, _p in db.calls if "count(DISTINCT action) FROM" in s)
    assert "action <> ALL(:ax_signin)" in totals_sql


def test_audit_activity_focus_and_sign_in_options():
    db = _audit_db()
    sec = run(db, "audit_activity", {"focus": "access", "include_sign_ins": True})
    assert sec["title"] == "Access-Control Audit Trail"
    sql = next(s for s, _p in db.calls if "jsonb_object_keys" in s)
    p = db.params_for("jsonb_object_keys")
    assert "split_part(action, '.', 1) = ANY(:fx_prefix)" in sql
    assert p["fx_prefix"] == ["user", "role", "auth"] and "signin" not in p
    assert not any("Sign-in Activity section" in n for n in sec["notes"])


# ── role_changes ────────────────────────────────────────────────────────────

def test_role_changes_diff_privileged_grants_and_rename():
    db = FakeDB({
        "FROM role_history": Result([
            (TS, "operator", "update", "admin", ["devices.manage", "users.manage"], ["alerts.manage"], 3,
             "Ticket 42", None, ["permissions"], "r-op"),
            (TS - timedelta(hours=1), "netops", "rename", "admin", [], [], 2, "", "network", ["name"], "r-net"),
        ]),
        "FROM roles": ROLES,
        "GROUP BY role": Result([("admin", 2, 2), ("viewer", 3, 4)]),
    })
    sec = run(db, "role_changes")
    assert kpi(sec, "Role changes")["value"] == "2"
    assert kpi(sec, "Permissions granted")["value"] == "2"
    assert kpi(sec, "Permissions revoked")["value"] == "1"
    grants = kpi(sec, "Privileged grants")
    assert grants["value"] == "1" and grants["accent"] == "danger"
    t = table(sec, "Role changes")
    first, second = t["rows"]
    assert first[col(t, "Change")] == "Updated (permissions)"
    assert first[col(t, "Permissions added")] == "devices.manage, users.manage"
    assert first[col(t, "Privileged added")] == "users.manage"
    assert first[col(t, "Time")] == "2026-10-07 12:00"
    assert second[col(t, "Change")] == "Renamed from network"
    current = table(sec, "Roles in force")
    admin = next(r for r in current["rows"] if r[0] == "Administrator")
    assert admin[col(current, "Active members")] == "2"


def test_role_changes_empty_period_still_lists_roles():
    db = FakeDB({"FROM roles": ROLES, "GROUP BY role": Result([])})
    sec = run(db, "role_changes")
    assert kpi(sec, "Role changes")["value"] == "0"
    assert table(sec, "Roles in force")["rows"]
    assert "No role was created" in sec["notes"][0]


# ── active_sessions ─────────────────────────────────────────────────────────

def test_active_sessions_mfa_ratio_and_no_addresses():
    db = FakeDB({
        "GROUP BY u.username": Result([
            ("alice", "admin", "local", 3, 1, "password", 2, TS, TS - timedelta(hours=5),
             NOW + timedelta(hours=8), 1),
            ("carol", "viewer", "oidc", 1, 0, "oidc", 1, TS, TS, NOW + timedelta(hours=8), 0),
        ]),
        "GROUP BY 1 ORDER BY 2 DESC": Result([("password", 3, 1, 1), ("oidc", 1, 0, 1)]),
        "FROM roles": ROLES,
    })
    sec = run(db, "active_sessions")
    assert kpi(sec, "Active sessions")["value"] == "4"
    assert kpi(sec, "Active sessions")["subtitle"] == "1 active in the last 15 minutes"
    mfa = kpi(sec, "MFA-verified")                 # password sessions only; SSO verifies at the provider
    assert mfa["value"] == "33.3%" and mfa["subtitle"] == "1 of 3 password sessions"
    assert kpi(sec, "Privileged sessions without MFA")["value"] == "2"
    accounts = table(sec, "Accounts with active sessions")
    alice = accounts["rows"][0]
    assert alice[col(accounts, "Role")] == "Administrator"
    assert alice[col(accounts, "MFA-verified")] == "1 of 3"
    assert alice[col(accounts, "Last activity")] == "Oct 07 12:00"
    assert db.params_for("GROUP BY u.username")["now"] == NOW
    assert "count(DISTINCT s.ip_address)" in db.sql() and "s.ip_address," not in db.sql()
    methods = table(sec, "Sessions by sign-in method")
    assert methods["rows"][1][0] == "Single sign-on (OIDC)"
    assert run(FakeDB({}), "active_sessions")["notes"] == ["No account is signed in right now."]


# ── license_usage ───────────────────────────────────────────────────────────

def _license_db(expires=NOW + timedelta(days=20), status="active"):
    return FakeDB({
        "FROM subscriptions": Result([("professional", status, NOW - timedelta(days=300), expires, 500, 200, 25)]),
        "via_controller": Result([(420, 30, 190, 24)]),
    })


def test_license_usage_limits_and_threshold(monkeypatch):
    monkeypatch.setattr(pack, "_remote_subscription", lambda: None)
    db = _license_db()
    sec = run(db, "license_usage")
    plan = kpi(sec, "Subscription")
    assert plan["value"] == "Active" and plan["subtitle"] == "Professional plan"
    days = kpi(sec, "Days remaining")
    assert days["value"] == "20" and days["accent"] == "warning"
    assert days["subtitle"] == "Expires 2026-10-27"
    dev = kpi(sec, "Devices")
    assert dev["value"] == "420 / 500" and dev["subtitle"] == "84.0% of license" and dev["accent"] == "warning"
    t = table(sec, "Licensed capacity")
    assert t["rows"][0] == ["Devices", "420", "500", "84.0%", "80", "warning"]
    assert any("30 controller-reported" in n for n in sec["notes"])
    assert "license_key" not in db.sql()
    relaxed = run(_license_db(), "license_usage", {"threshold": 90})
    assert table(relaxed, "Licensed capacity")["rows"][0][-1] == "ok"
    assert table(relaxed, "Licensed capacity")["rows"][1][-1] == "warning"     # 95 % of checks


def test_license_usage_expired_and_remote_overlay(monkeypatch):
    monkeypatch.setattr(pack, "_remote_subscription", lambda: None)
    sec = run(_license_db(expires=NOW - timedelta(days=1)), "license_usage")
    assert kpi(sec, "Subscription")["value"] == "Expired" and kpi(sec, "Subscription")["accent"] == "danger"
    assert kpi(sec, "Days remaining")["value"] == "0"
    monkeypatch.setattr(pack, "_remote_subscription", lambda: {
        "plan": "enterprise", "max_devices": 10000, "expires_at": None, "is_active": True})
    remote = run(_license_db(), "license_usage")
    assert kpi(remote, "Subscription")["subtitle"] == "Enterprise plan"
    assert kpi(remote, "Days remaining")["value"] == "No expiry"
    assert kpi(remote, "Devices")["value"] == "420 / 10,000"
    assert kpi(remote, "Users")["value"] == "24 / 100"


# ── ClickHouse inventory: retention coverage and storage ────────────────────

EPOCH = datetime(1970, 1, 1)


def _inventory_ch(fail=False):
    def ttl(days):
        return f"MergeTree PARTITION BY toYYYYMM(timestamp) TTL toDateTime(timestamp) + toIntervalDay({days})"
    naive = NOW.replace(tzinfo=None)
    return FakeCH({
        "FROM system.tables": [
            ("ping_metrics", "MergeTree", ttl(30)), ("ping_metrics_5m", "MergeTree", ttl(90)),
            ("ping_metrics_1h", "MergeTree", ttl(400)), ("device_status_log", "MergeTree", ttl(180)),
            ("apm_spans", "MergeTree", ttl(14)), ("schema_migrations", "MergeTree", ""),
        ],
        "FROM system.parts": [
            ("ping_metrics", 6 * 1024 ** 3, 9_000_000, naive - timedelta(days=30, hours=12), naive, None, None),
            ("ping_metrics_5m", 2 * 1024 ** 3, 1_000_000, naive - timedelta(days=20), naive, None, None),
            ("ping_metrics_1h", 1024 ** 3, 100_000, naive - timedelta(days=401), naive, None, None),
            ("device_status_log", 1024 ** 2, 5_000, EPOCH, EPOCH,
             (naive - timedelta(days=10)).date(), naive.date()),
        ],
    }, fail=fail)


def test_data_retention_coverage_windows(monkeypatch):
    db = FakeDB({"FROM audit_logs": Result([(NOW - timedelta(days=100), NOW - timedelta(days=50),
                                             NOW - timedelta(days=29), None, NOW - timedelta(days=5),
                                             NOW - timedelta(days=200))])})
    sec = run(db, "data_retention_coverage", ch=_inventory_ch(), monkeypatch=monkeypatch)
    assert kpi(sec, "Raw metrics cover")["value"] == "30 days"
    five = kpi(sec, "5-minute rollups cover")
    assert five["value"] == "20 days" and five["accent"] == "warning"
    assert kpi(sec, "Hourly rollups cover")["value"] == "400 days"
    assert kpi(sec, "Event history covers")["value"] == "10 days"     # from min_date (min_time unset)
    assert kpi(sec, "Time-series data")["value"] == "9.0 GB"
    cats = table(sec, "Coverage by data category")
    raw = next(r for r in cats["rows"] if r[0] == "Raw metrics")
    assert raw[col(cats, "Retention")] == "30 days"
    assert [raw[col(cats, h)] for h in ("24 h", "7 d", "30 d", "90 d", "1 y")] == \
        ["Full", "Full", "Full", "30 d", "30 d"]
    apm = next(r for r in cats["rows"] if r[0] == "APM traces")
    assert apm[col(cats, "Oldest data")] == "No data" and apm[col(cats, "30 d")] == "No data"
    ds = table(sec, "Coverage by dataset")
    assert "schema_migrations" not in [r[1] for r in ds["rows"]]
    ping = next(r for r in ds["rows"] if r[1] == "ping_metrics")
    assert ping[0] == "Ping results" and ping[col(ds, "Oldest data")] == "2026-09-07"
    assert ping[col(ds, "Newest data")] == "Oct 07 15:00"
    status_log = next(r for r in ds["rows"] if r[1] == "device_status_log")
    assert status_log[col(ds, "Newest data")] == "2026-10-07"
    pg = table(sec, "Application history")
    assert pg["title"] == "Application history"                 # no database engine name
    audit = pg["rows"][0]
    assert audit[:3] == ["Audit log", "Kept until deleted", "2026-06-29"] and audit[-1] == "100 d"
    sensors = next(r for r in pg["rows"] if r[0] == "Sensor status changes")
    assert sensors[1] == "30 days" and sensors[col(pg, "30 d")] == "29 d"
    trimmed = run(db, "data_retention_coverage", {"top_n": 2}, ch=_inventory_ch(), monkeypatch=monkeypatch)
    assert table(trimmed, "Coverage by dataset")["title"].endswith("top 2 of 5 datasets")


def test_storage_utilization_volume_categories_and_events(monkeypatch):
    monkeypatch.setattr(pack, "_data_volume", lambda: {
        "usage_percent": 78.4, "used_bytes": 784 * 1024 ** 3, "total_bytes": 1000 * 1024 ** 3,
        "free_bytes": 216 * 1024 ** 3})

    def setting(sql, params):
        return Result([({"auto_purge": {"enabled": True, "threshold_pct": 85, "target_pct": 75,
                                        "min_keep_days": 7}},)])

    db = FakeDB({
        "FROM system_settings": setting,
        "pg_database_size": Result(scalar=5 * 1024 ** 3),
        "GROUP BY 1": Result([("auto_purge", 2, 1024 ** 3), ("retention_applied", 1, 0)]),
        "ORDER BY created_at DESC LIMIT :lim": Result([
            (TS, "auto_purge", "system", 1024 ** 3, None, "87.0", "74.0", None, 3, None, None),
            (TS - timedelta(days=1), "retention_applied", "admin", 0, None, None, None, None, None, None, 4),
        ]),
    })
    sec = run(db, "storage_utilization", ch=_inventory_ch(), monkeypatch=monkeypatch)
    vol = kpi(sec, "Data volume used")
    assert vol["value"] == "78.4%" and vol["accent"] == "warning"
    # Customer-facing category, never the engine name.
    assert kpi(sec, "Configuration and event database")["value"] == "5.0 GB"
    assert not any("PostgreSQL" in k["label"] or "ClickHouse" in k["label"] for k in sec["kpis"])
    assert not any("PostgreSQL" in t["title"] or "ClickHouse" in t["title"] for t in sec["tables"])
    assert sec["email_scope"] == "This appliance"
    # 78.4% is within 10 points of the 85% purge threshold: a warning to act on.
    assert [(f["severity"], f["text"]) for f in sec["findings"]] == [
        ("warning", "The data volume is 78.4% full (automatic purge at 85%)")]
    assert sec["findings"][0]["path"] == "/settings/general?tab=storage"
    assert kpi(sec, "Purges in period")["value"] == "2"
    assert kpi(sec, "Space freed")["value"] == "1.0 GB"
    assert kpi(sec, "Automatic purge")["value"] == "At 85%"
    cats = table(sec, "Time-series storage by category")
    assert cats["rows"][0][0] == "Raw metrics" and cats["rows"][0][col(cats, "Size")] == "6.0 GB"
    ev = table(sec, "Storage events in the period")
    assert ev["rows"][0][col(ev, "Detail")] == "volume 87.0% → 74.0%; 3 partition(s) dropped"
    assert ev["rows"][0][col(ev, "Time")] == "2026-10-07 12:00"
    assert ev["rows"][1][col(ev, "Detail")] == "4 table(s) changed"
    assert sec["charts"][0]["series"]["unit"] == "B"
    # Event details are read field by field: free-text errors are never selected.
    assert "SELECT details" not in db.sql()
    down = run(db, "storage_utilization", ch=_inventory_ch(fail=True), monkeypatch=monkeypatch)
    assert any("did not respond" in n for n in down["notes"])
    assert kpi(down, "Time-series data")["value"] == "—"


# ── backup_status ───────────────────────────────────────────────────────────

def _backup_db(last_ok_age=timedelta(days=2), schedule=True):
    def setting(sql, params):
        if params.get("key") == "storage.backup_schedule" and schedule:
            return Result([({"enabled": True, "frequency": "weekly", "weekday": 6, "hour_utc": 2,
                             "include_clickhouse": False, "keep_last": 5},)])
        return Result([])

    last = NOW - last_ok_age
    return FakeDB({
        "FROM system_settings": setting,
        "count(*) FROM storage_backups": Result([(last, 4, 3 * 1024 ** 3, 0, 5)]),
        "ORDER BY COALESCE(finished_at, created_at) DESC LIMIT 1":
            Result([(last, "config", False, 1024 ** 3, "schedule")]),
        "'backup_created', 'backup_failed'": Result([("backup_created", 3), ("backup_failed", 1)]),
        "AS in_window": Result([
            (last - timedelta(minutes=4), last, "schedule", "config", "completed", False, 1024 ** 3, "",
             None, None, True),
            (NOW - timedelta(days=3), NOW - timedelta(days=3), "admin", "full", "failed", True, 0,
             "pg_dump failed: report to ops@example.com", NOW - timedelta(days=1), "completed", True),
        ]),
    })


def test_backup_status_age_failures_and_schedule():
    sec = run(_backup_db(), "backup_status")
    last = kpi(sec, "Since last successful backup")
    assert last["value"] == "2 d 0 h" and last["accent"] == "success"
    assert last["subtitle"] == "2026-10-05 15:00"
    assert kpi(sec, "Backups in period")["value"] == "3"
    assert kpi(sec, "Failed backups")["value"] == "1" and kpi(sec, "Failed backups")["accent"] == "danger"
    assert kpi(sec, "Time-series data included")["value"] == "No"
    sched = kpi(sec, "Schedule")
    assert sched["value"] == "Weekly" and sched["subtitle"] == "Next Oct 11 05:00 · keeps 5"
    t = table(sec, "Backups")
    assert t["rows"][0][col(t, "Status")] == "success" and t["rows"][0][col(t, "Duration")] == "4 min"
    failed = t["rows"][1]
    assert failed[col(t, "Status")] == "failed"
    assert failed[col(t, "Error")] == "pg_dump failed: report to <address>"
    assert failed[col(t, "Type")] == "Full (with time-series data)"


def test_backup_status_overdue_option_and_never():
    sec = run(_backup_db(), "backup_status", {"max_age_days": 1})
    assert kpi(sec, "Since last successful backup")["accent"] == "danger"
    assert [f["severity"] for f in sec["findings"]] == ["danger", "danger"]   # overdue, 1 failed
    assert sec["findings"][0]["text"] == "The last catalogued backup is 2 d 0 h old (overdue after 1 days)"
    never = run(FakeDB({}), "backup_status")
    assert kpi(never, "Since last successful backup")["value"] == "None catalogued"
    assert kpi(never, "Schedule")["value"] == "Off"
    # Only the ZenPlus catalogue is read: say so, never "no backup has been taken".
    note = never["notes"][0]
    assert note.startswith("No backup is recorded in the ZenPlus backup catalogue (Settings → Storage → "
                           "Backups) as of ")
    assert note.endswith("Pre-upgrade snapshots and external backups are not listed in the catalogue.")
    assert not any("has been taken" in n for n in never["notes"])
    assert [(f["severity"], f["text"]) for f in never["findings"]] == [
        ("danger", "No backup is recorded in the ZenPlus backup catalogue")]


def test_backup_status_healthy_has_no_findings():
    sec = run(_backup_db(), "backup_status")
    # Recent backup, schedule on; the one failed backup in the period is the only action.
    assert [(f["severity"], f["text"]) for f in sec["findings"]] == [
        ("danger", "1 backup(s) failed in the period")]
    assert any("Pre-upgrade snapshots and external backups are not listed" in n for n in sec["notes"])


# ── report_delivery ─────────────────────────────────────────────────────────

def _delivery_db():
    def totals(sql, params):
        if params["f"] < datetime(2026, 9, 30, tzinfo=timezone.utc):
            return Result([(8, 8, 0, 0)])
        return Result([(10, 8, 1, 1)])

    day = int(datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc).timestamp())
    return FakeDB({
        "SELECT schedule_id::text": Result([("sch1", 7, 6, 1, 0, TS), ("sch2", 2, 1, 0, 1, TS),
                                            (None, 1, 1, 0, 0, TS)]),
        "SELECT error FROM report_runs": Result([
            ("Ops mail: no SMTP gateway",),
            ("2 direct recipient(s): (550, 'mailbox unavailable') for bob@example.com; "
             "Ops mail: no SMTP gateway",)]),
        "AS b, status": Result([(day, "success", 8), (day, "failed", 1)]),
        "FROM report_schedules": Result([
            ("sch1", "Weekly SLA", True, "weekly", "availability", TS, "success", NOW + timedelta(days=3), False),
            ("sch2", "Daily NOC", True, "daily", "down_degraded_nodes", TS, "failed", NOW - timedelta(hours=1),
             False),
            ("sch3", "Paused one", False, "monthly", "custom", None, None, None, True),
        ]),
        "FROM report_runs": totals,
    })


def test_report_delivery_schedules_reasons_and_overdue():
    sec = run(_delivery_db(), "report_delivery")
    assert kpi(sec, "Report runs")["value"] == "10"
    assert kpi(sec, "Report runs")["subtitle"] == "▲ 2 vs previous period"
    assert kpi(sec, "Delivered")["value"] == "80.0%"
    assert kpi(sec, "Failed")["value"] == "1"
    od = kpi(sec, "Overdue schedules")
    assert od["value"] == "1" and od["accent"] == "danger" and od["subtitle"] == "of 2 enabled"
    t = table(sec, "Delivery by schedule")
    first = t["rows"][0]
    assert first[0] == "Daily NOC" and first[col(t, "Report")] == "Down and Degraded Nodes"
    assert first[col(t, "Delivered")] == "50.0%" and first[col(t, "Partial / failed")] == "0 / 1"
    assert first[col(t, "Last status")] == "failed"
    adhoc = next(r for r in t["rows"] if r[0] == "On demand")
    assert adhoc[col(t, "Runs")] == "1"
    paused = next(r for r in t["rows"] if r[0] == "Paused one")
    assert paused[col(t, "Next run")] == "Paused" and paused[col(t, "Runs")] == "0"
    assert paused[col(t, "Report")] == "Organisation template"
    reasons = table(sec, "Why deliveries failed")
    assert reasons["rows"][0][:2] == ["No SMTP gateway configured", "2"]
    rejected = next(r for r in reasons["rows"] if r[0] == "Recipient rejected")
    assert "<address>" in rejected[2] and "example.com" not in rejected[2]
    overdue = table(sec, "Overdue schedules")
    assert overdue["rows"][0][:3] == ["Daily NOC", "Daily", "2026-10-07 14:00"]
    chart = sec["charts"][0]["series"]
    assert [(s["name"], s["color"]) for s in chart["series"]] == \
        [("Delivered", "success"), ("Partial", "warning"), ("Failed", "danger")]


def test_delivery_failure_reasons_most_specific_first():
    assert pack._failure_reason("2 direct recipient(s): timed out") == "Mail server unreachable or timed out"
    assert pack._failure_reason("bob@x: (550, 'mailbox unavailable')") == "Recipient rejected"
    assert pack._failure_reason("1 direct recipient(s): (535, 'authentication failed')") == \
        "Mail server rejected the sign-in"
    assert pack._failure_reason("The PDF attachment could not be generated (OSError)") == \
        "Attachment not generated"
    assert pack._failure_reason("Ops mail: no SMTP gateway") == "No SMTP gateway configured"
    assert pack._failure_reason("something odd") == "Other"


def test_report_delivery_top_n_and_empty():
    sec = run(_delivery_db(), "report_delivery", {"top_n": 2})
    assert table(sec, "Delivery by schedule")["title"] == "Delivery by schedule — top 2 of 4 schedules"
    empty = run(FakeDB({}), "report_delivery")
    assert empty["notes"] == ["No report was generated or scheduled in this period."]
