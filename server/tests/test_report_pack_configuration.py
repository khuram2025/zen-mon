"""Configuration (NCCM) section pack (report_packs/configuration.py) with canned data.

Postgres is a fake answering by SQL substring; nothing here connects to a
database. Covers firmware/OS version distribution (older and minority
versions, no end-of-life claims), hardware serial inventory, the fleet
compliance trend, compliance exceptions, the change audit (unverified
reported user, captured changes, first captures), restore readiness, backup
failure incidents and credential hygiene, plus device filters, options,
organisation-timezone formatting, the built-in templates and the rule that
no query reads configuration text or secrets.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.scoping import NO_MATCH_ID
from app.services import report_sections as rs
from app.services import report_templates as rt
from app.services.branding import Branding
from app.services.report_packs import configuration as pack

RIYADH = "Asia/Riyadh"          # UTC+3, no DST
# A calendar month in the organisation timezone: Sep 1 00:00 to Oct 1 00:00
# Riyadh time, as naive UTC (what the render API passes).
FRM = datetime(2026, 8, 31, 21, 0)
TO = datetime(2026, 9, 30, 21, 0)
A, B = "aaaaaaaa-0000-0000-0000-00000000000a", "bbbbbbbb-0000-0000-0000-00000000000b"

PACK_IDS = ("firmware_inventory", "hardware_serial_inventory", "ncm_compliance_trend",
            "ncm_compliance_exceptions", "ncm_change_audit", "ncm_restore_readiness",
            "ncm_backup_incidents", "ncm_credential_hygiene")


# ── fakes ───────────────────────────────────────────────────────────────────

class _Rows:
    rowcount = 0

    def __init__(self, rows):
        self.rows = list(rows)

    def fetchall(self):
        return list(self.rows)

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return self.rows[0][0] if self.rows else None

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.rows)


class FakeDB:
    """Answers with the rows of the first route whose key occurs in the SQL."""

    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        for key, rows in self.routes:
            if key in sql:
                return _Rows(rows(sql, params or {}) if callable(rows) else rows)
        return _Rows([])

    def begin_nested(self):
        from tests.conftest import _NullTransaction
        return _NullTransaction()

    def sql_with(self, needle: str) -> list[tuple[str, dict]]:
        return [c for c in self.calls if needle in c[0]]


class NoCH:
    def query(self, *a, **k):          # the pack never uses ClickHouse
        raise AssertionError("configuration sections must not query ClickHouse")


def run(monkeypatch, db, entry, frm=FRM, to=TO, device_ids=None):
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: NoCH())
    entries = [entry if isinstance(entry, dict) else {"id": entry}]
    out = asyncio.run(rs.build_sections(db, entries, frm, to,
                                        {"device_ids": device_ids} if device_ids else None,
                                        requester=None, branding=Branding(timezone=RIYADH)))
    assert len(out) == 1
    sec = out[0]
    assert not sec.get("error"), sec["notes"]
    for t in sec["tables"]:
        assert len(t["styles"]) == len(t["headers"])
        assert all(len(r) == len(t["headers"]) for r in t["rows"]), t["title"]
        assert all(isinstance(c, str) for r in t["rows"] for c in r), t["title"]
    for k in sec["kpis"]:
        assert isinstance(k["value"], str)
    assert sec.get("description")
    return sec


def kpi(sec, label_start):
    return next(k for k in sec["kpis"] if k["label"].startswith(label_start))


def table(sec, title_start):
    return next(t for t in sec["tables"] if t["title"].startswith(title_start))


def utcnow():
    return datetime.now(timezone.utc)


def riyadh_short(dt: datetime) -> str:
    return Branding(timezone=RIYADH).fmt_short(dt)


def riyadh_date(dt: datetime) -> str:
    return Branding(timezone=RIYADH).fmt_date(dt)


# ── firmware_inventory ─────────────────────────────────────────────────────

def _firmware_db():
    def dev(i, name, vendor, model, version):
        return (f"{i:08d}-0000-0000-0000-000000000000", name, f"10.0.0.{i}", vendor, model, version)
    inventory = (
        [dev(i, f"acc-sw{i}", "Cisco", "C9300-48P", "17.9.4a") for i in range(1, 6)]
        + [dev(6, "acc-sw6", "Cisco", "C9300-48P", "17.6.5")]
        + [dev(7, "lab-sw7", "Cisco", "WS-C2960X", "15.2(7)E3"),
           dev(8, "lab-sw8", "Cisco", "WS-C2960X", "15.2(7)E3"),
           dev(9, "lab-sw9", "Cisco", "WS-C2960X", "15.2(7)E10"),
           dev(10, "fw-edge", "Fortinet", "FortiGate", None),
           dev(11, "pa-dc", "Palo Alto Networks", "PA-VM", "10.2.4")])

    def answer(sql, p):
        ids = p.get("dev_ids")
        return [r for r in inventory if not ids or r[0] in ids]
    return FakeDB([("FROM devices d WHERE TRUE", answer)])


def test_firmware_inventory_flags_older_and_minority_versions(monkeypatch):
    db = _firmware_db()
    sec = run(monkeypatch, db, "firmware_inventory")
    assert sec["title"] == "Firmware and OS Versions"
    assert kpi(sec, "Devices reporting a version")["value"] == "10 of 11"
    assert kpi(sec, "Devices reporting a version")["subtitle"] == "1 not reported"
    assert kpi(sec, "Distinct versions")["value"] == "5"
    # 17.6.5 is older than 17.9.4a and a minority (1 of 6 = 16.7% < 20%);
    # 15.2(7)E3 sorts below 15.2(7)E10 (natural order), on 2 of 3 devices.
    assert kpi(sec, "On older versions")["value"] == "3"
    assert kpi(sec, "On minority versions")["value"] == "1"

    dist = table(sec, "Version distribution")
    c2960 = [r for r in dist["rows"] if r[1] == "WS-C2960X"]
    assert [r[2] for r in c2960] == ["15.2(7)E10", "15.2(7)E3"]       # newest first
    assert c2960[0][-1] == "ok" and c2960[1][-1] == "stale"
    old = next(r for r in dist["rows"] if r[2] == "17.6.5")
    assert old[3:5] == ["1", "16.7%"]
    assert old[-1] == "stale" and "Minority: 1 of 6 devices" in old[5]
    assert next(r for r in dist["rows"] if r[2] == "10.2.4")[5] == "Only version on this model"

    listed = table(sec, "Devices on older or minority versions")
    names = [r[0] for r in listed["rows"]]
    assert set(names) == {"acc-sw6", "lab-sw7", "lab-sw8"}             # newest versions not listed
    assert next(r for r in listed["rows"] if r[0] == "acc-sw6")[5] == "17.9.4a"
    # No end-of-life claims: the gap is disclosed instead.
    assert any("No end-of-life" in n for n in sec["notes"])
    assert any("1 device(s) report no OS version" in n for n in sec["notes"])
    assert sec["description"].startswith("OS and firmware versions of 11 device(s)")
    assert sec["charts"][0]["series"]["kind"] == "rank"
    assert sec["charts"][0]["series"]["points"][0] == {"label": "Cisco 17.9.4a", "value": 5}


def test_firmware_inventory_options(monkeypatch):
    db = _firmware_db()
    sec = run(monkeypatch, db, {"id": "firmware_inventory", "options": {"list": "all", "top_n": 4}})
    listed = table(sec, "Devices and their versions")
    assert len(listed["rows"]) == 4 and "top 4 of 11" in listed["title"]
    assert "top 4 of" in table(sec, "Version distribution")["title"]

    sec = run(monkeypatch, db, {"id": "firmware_inventory", "options": {"list": "all"}})
    missing = next(r for r in table(sec, "Devices and their versions")["rows"] if r[0] == "fw-edge")
    assert missing[4:] == ["—", "—", "Not reported", "unknown"]

    db = _firmware_db()
    sec = run(monkeypatch, db, {"id": "firmware_inventory", "options": {"version": "firmware"}})
    assert sec["title"] == "Chassis Firmware Revisions"
    sql = db.calls[0][0]
    assert "firmware_version" in sql and "os_version" not in sql

    sec = run(monkeypatch, _firmware_db(),
              {"id": "firmware_inventory", "options": {"minority_pct": 50}})
    # At 50% the single 15.2(7)E10 device (1 of 3) becomes a minority even though
    # it is the newest; the 2-of-3 15.2(7)E3 devices are not.
    assert kpi(sec, "On minority versions")["value"] == "2"
    e10 = next(r for r in table(sec, "Version distribution")["rows"] if r[2] == "15.2(7)E10")
    assert e10[5] == "Minority: 1 of 3 devices" and e10[6] == "warning"


def test_firmware_inventory_device_filter(monkeypatch):
    db = _firmware_db()
    sec = run(monkeypatch, db, "firmware_inventory",
              device_ids=["00000006-0000-0000-0000-000000000000"])
    assert db.calls[0][1]["dev_ids"] == ["00000006-0000-0000-0000-000000000000"]
    assert "= ANY(:dev_ids)" in db.calls[0][0]
    assert kpi(sec, "Devices reporting a version")["value"] == "1 of 1"
    # A single version on its model is neither older nor a minority.
    assert kpi(sec, "On older versions")["value"] == "0"

    db = FakeDB()
    sec = run(monkeypatch, db, "firmware_inventory", device_ids=[NO_MATCH_ID])
    assert sec["notes"][0] == "No devices in scope."
    assert db.calls[0][1]["dev_ids"] == [NO_MATCH_ID]


# ── hardware_serial_inventory ───────────────────────────────────────────────

def _hardware_db(listing=None, dupes=None):
    seen = datetime(2026, 10, 7, 9, 30, tzinfo=timezone.utc)
    rows = listing if listing is not None else [
        ("core-sw1", "chassis", "Switch 1", "C9300-48P", "FOC2233X0AB", "V02", "17.9.4a", seen, False),
        ("core-sw1", "powerSupply", "PS 1", "PWR-C1-715WAC", "", None, None, seen, False),
        ("core-sw1", "module", "Uplink module", "C9300-NM-8X", "FOC1111", "V01", "", seen, True),
    ]
    return FakeDB([
        ("FILTER (WHERE d.snmp_enabled)", [(5, 4)]),
        ("GROUPING(e.class)", [("chassis", 0, 3, 3, 0, 0), ("powerSupply", 0, 2, 2, 1, 0),
                               ("module", 0, 4, 2, 1, 1), (None, 1, 9, 3, 2, 1)]),
        ("ORDER BY 1, e.device_id, e.ent_index", rows),
        ("HAVING count(DISTINCT e.device_id) > 1",
         dupes if dupes is not None else [("FOC2233X0AB", 2, "core-sw1, core-sw1-old")]),
    ])


def test_hardware_serial_inventory_lists_components(monkeypatch):
    db = _hardware_db()
    sec = run(monkeypatch, db, "hardware_serial_inventory")
    assert kpi(sec, "Devices with hardware inventory")["value"] == "3 of 5"
    assert kpi(sec, "Devices with hardware inventory")["subtitle"] == "4 polled over SNMP"
    assert kpi(sec, "Chassis")["value"] == "3"
    assert kpi(sec, "Components listed")["value"] == "9"
    assert kpi(sec, "Missing serial numbers")["value"] == "2"
    assert kpi(sec, "Duplicate chassis serials")["value"] == "1"
    t = table(sec, "Hardware components")
    assert "top 3 of 9" in t["title"]
    assert t["headers"][-1] == "Last seen"                          # no State column by default
    chassis, psu, module = t["rows"]
    assert chassis[:5] == ["core-sw1", "Chassis", "Switch 1", "C9300-48P", "FOC2233X0AB"]
    assert chassis[-1] == "Oct 07 12:30"                             # Riyadh time
    assert psu[1] == "Power supply" and psu[4] == "—" and psu[5] == "—"   # blank is not measured
    assert table(sec, "Chassis serial numbers reported by more than one device")["rows"] == [
        ["FOC2233X0AB", "2", "core-sw1, core-sw1-old"]]
    # Components missing from the latest walk are filtered in SQL by default.
    listing_sql, params = db.sql_with("ORDER BY 1, e.device_id")[0]
    assert "NOT (e.last_seen < w.walk - interval '1 hour')" in listing_sql
    assert params["classes"] == ["chassis", "module", "powerSupply", "fan", "stack"]
    assert sec["charts"][0]["series"]["kind"] == "donut"


def test_hardware_serial_inventory_options_and_filter(monkeypatch):
    db = _hardware_db()
    sec = run(monkeypatch, db, {"id": "hardware_serial_inventory",
                                "options": {"components": "chassis", "include_absent": True}})
    sql, params = db.sql_with("ORDER BY 1, e.device_id")[0]
    assert params["classes"] == ["chassis"]
    assert "NOT (e.last_seen" not in sql
    t = table(sec, "Hardware components")
    assert t["headers"][-1] == "State"
    assert [r[-1] for r in t["rows"]] == ["ok", "ok", "stale"]
    assert kpi(sec, "Missing from latest walk")["value"] == "1"

    db = _hardware_db()
    run(monkeypatch, db, {"id": "hardware_serial_inventory", "options": {"components": "all"}})
    assert "classes" not in db.sql_with("ORDER BY 1, e.device_id")[0][1]

    db = _hardware_db()
    run(monkeypatch, db, "hardware_serial_inventory", device_ids=[A])
    assert all(p.get("dev_ids") == [A] for _s, p in db.calls)
    # The per-device "latest walk" subquery is filtered too.
    assert "WHERE TRUE AND device_id::text = ANY(:dev_ids)" in db.sql_with("GROUPING")[0][0]


def test_hardware_serial_inventory_empty(monkeypatch):
    db = FakeDB([("FILTER (WHERE d.snmp_enabled)", [(4, 2)])])
    sec = run(monkeypatch, db, "hardware_serial_inventory")
    assert kpi(sec, "Devices with hardware inventory")["value"] == "0 of 4"
    assert "No hardware inventory has been collected" in sec["notes"][0]
    assert not sec["tables"]


# ── ncm_compliance_trend ───────────────────────────────────────────────────

def _trend_db():
    d1, d30 = date(2026, 9, 1), date(2026, 9, 30)
    return FakeDB([("FROM ncm_compliance_snapshots", [
        (d1, "Cisco hardening", 10, 80, 20, 0, 0, 6),
        (d1, "Baseline", 5, 45, 5, 2, 1, 3),
        (d30, "Cisco hardening", 10, 95, 5, 0, 0, 8),
        (d30, "Baseline", 5, 50, 0, 0, 1, 5),
    ])])


def test_compliance_trend_rates_and_change(monkeypatch):
    db = _trend_db()
    sec = run(monkeypatch, db, "ncm_compliance_trend")
    # The window covers the organisation's calendar month (Riyadh days).
    assert db.calls[0][1] == {"d0": date(2026, 9, 1), "d1": date(2026, 9, 30)}
    assert kpi(sec, "Pass rate")["value"] == "96.67%"                # 145 / (145 + 5)
    assert kpi(sec, "Pass rate")["subtitle"] == "▲ 13.33 pts since 2026-09-01"
    assert kpi(sec, "Failed checks")["value"] == "5"
    assert kpi(sec, "Failed checks")["subtitle"] == "▼ 20 since 2026-09-01"
    assert kpi(sec, "Not assessable")["value"] == "0"
    assert kpi(sec, "Excepted checks")["value"] == "1"
    assert kpi(sec, "Compliant device–policy pairs")["value"] == "13 of 15"
    assert kpi(sec, "Days recorded")["value"] == "2 of 30"

    rate, outcomes = sec["charts"]
    pts = rate["series"]["points"]
    assert len(pts) == 30 and pts[0]["v"] == 83.33 and pts[-1]["v"] == 96.67
    assert pts[1]["v"] is None                                       # unrecorded day = gap
    assert rate["series"]["y_domain"] == [75.0, 100]
    assert outcomes["series"]["kind"] == "stacked"
    assert [s["name"] for s in outcomes["series"]["series"]] == [
        "Passed", "Failed", "Not assessable", "Excepted"]
    assert outcomes["series"]["series"][1]["points"][1]["v"] is None

    pol = table(sec, "By policy")
    assert [r[0] for r in pol["rows"]] == ["Cisco hardening", "Baseline"]   # failing first
    hardening, baseline = pol["rows"]
    assert hardening[2:6] == ["80.00%", "95.00%", "+15.00 pts", "5"]
    assert hardening[-1] == "non-compliant" and baseline[-1] == "compliant"
    daily = table(sec, "Daily snapshots")
    assert daily["rows"][0][0] == "2026-09-30"                       # newest first
    assert daily["rows"][1][-1] == "83.33%"
    assert not any("device selection" in n for n in sec["notes"])


def test_compliance_trend_options_filter_and_empty(monkeypatch):
    sec = run(monkeypatch, _trend_db(), {"id": "ncm_compliance_trend", "options": {"chart": "rate"}})
    assert [c["title"] for c in sec["charts"]] == ["Pass rate per day"]
    sec = run(monkeypatch, _trend_db(), {"id": "ncm_compliance_trend",
                                         "options": {"chart": "outcomes"}})
    assert [c["title"] for c in sec["charts"]] == ["Check outcomes per day"]

    # Snapshots are fleet-wide: a device selection is disclosed, not applied.
    db = _trend_db()
    sec = run(monkeypatch, db, "ncm_compliance_trend", device_ids=[A])
    assert any("device selection does not apply" in n for n in sec["notes"])
    assert rs.SECTION_REGISTRY["ncm_compliance_trend"]["scope"] == "fleet"

    sec = run(monkeypatch, FakeDB(), "ncm_compliance_trend")
    assert sec["notes"][0].startswith("No compliance snapshots were recorded")
    assert not sec["kpis"] and not sec["charts"]


def test_compliance_trend_never_asks_for_future_days(monkeypatch):
    now = utcnow()
    db = _trend_db()
    run(monkeypatch, db, "ncm_compliance_trend",
        frm=(now - timedelta(days=2)).replace(tzinfo=None), to=(now + timedelta(days=3)).replace(tzinfo=None))
    assert db.calls[0][1]["d1"] <= now.date()


# ── ncm_compliance_exceptions ──────────────────────────────────────────────

def _exceptions_db(now):
    active = [
        ("SSH version 2 only", "high", "Cisco hardening", False, "core-sw1", "Network team",
         "Legacy line card awaiting replacement", now - timedelta(days=30),
         now + timedelta(days=5, hours=1), 1),
        ("NTP servers configured", "medium", "Baseline", True, None, "Operations",
         "NTP migration in progress", now - timedelta(days=10), now + timedelta(days=60), 12),
    ]
    lapsed = [("Login banner present", "low", "Baseline", False, "edge-rtr1", "Security",
               "Temporary", now - timedelta(days=40), now - timedelta(days=3), 0)]

    def answer(sql, p):
        return lapsed if "e.expires_at <= now()" in sql else active
    return FakeDB([("FROM ncm_compliance_exceptions e", answer)])


def test_compliance_exceptions(monkeypatch):
    now = utcnow()
    db = _exceptions_db(now)
    sec = run(monkeypatch, db, "ncm_compliance_exceptions")
    assert kpi(sec, "Active exceptions")["value"] == "2"
    assert kpi(sec, "Expiring within 14 days")["value"] == "1"
    assert kpi(sec, "Lapsed in the last 14 days")["value"] == "1"
    assert kpi(sec, "Fleet-wide")["value"] == "1"
    assert kpi(sec, "Critical or major rules")["value"] == "1"       # high -> major
    assert kpi(sec, "Checks excepted")["value"] == "13"
    t = table(sec, "Active exceptions")
    ssh, ntp = t["rows"]
    assert ssh[:4] == ["SSH version 2 only (Cisco hardening)", "major", "core-sw1", "Network team"]
    assert ssh[5] == riyadh_date(now + timedelta(days=5, hours=1))
    assert ssh[6] == "5 d" and ssh[7] == "1" and ssh[8] == "expiring"
    assert ntp[1] == "warning" and ntp[2] == "All devices" and ntp[-1] == "active"
    lapsed = table(sec, "Lapsed in the last 14 days")
    assert lapsed["rows"][0][1] == "minor" and lapsed["rows"][0][-1] == "expired"
    assert db.calls[1][1]["days"] == 14


def test_compliance_exceptions_options_and_filter(monkeypatch):
    now = utcnow()
    db = _exceptions_db(now)
    sec = run(monkeypatch, db, {"id": "ncm_compliance_exceptions", "options": {"days": 3, "top_n": 1}})
    assert kpi(sec, "Expiring within 3 days")["value"] == "0"
    assert all(p["days"] == 3 for _s, p in db.calls)
    t = table(sec, "Active exceptions")
    assert len(t["rows"]) == 1 and "top 1 of 2" in t["title"]

    db = _exceptions_db(now)
    sec = run(monkeypatch, db, "ncm_compliance_exceptions", device_ids=[A])
    for sql, params in db.calls:
        assert params["dev_ids"] == [A]
        assert "e.device_id IS NULL OR e.device_id::text = ANY(:dev_ids)" in sql
        assert "r.device_id::text = ANY(:dev_ids)" in sql      # excepted checks counted in scope
    assert any("Fleet-wide exceptions apply" in n for n in sec["notes"])

    sec = run(monkeypatch, FakeDB(), "ncm_compliance_exceptions")
    assert sec["notes"] == ["No active compliance exceptions, and none lapsed in the last 14 days."]


# ── ncm_change_audit ────────────────────────────────────────────────────────

def _riyadh_midnight_epoch(y, m, d):
    from zoneinfo import ZoneInfo
    return int(datetime(y, m, d, tzinfo=ZoneInfo(RIYADH)).timestamp())


def _audit_db():
    t1 = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
    events = [
        (t1, "core-sw1", "syslog", "Cisco/Arista configuration-change message", "admin",
         True, True, "success", True, t1 + timedelta(minutes=4)),
        (t1 + timedelta(hours=1), "edge-rtr1", "trap", "CISCO-CONFIG-MAN-MIB ciscoConfigManEvent trap",
         None, True, True, "success", False, t1 + timedelta(hours=1, minutes=3)),
        (t1 + timedelta(hours=2), "core-sw1", "api", "CHG-1234 approved window", "jdoe",
         False, False, None, None, None),
        (t1 + timedelta(hours=3), "lab-sw9", "syslog", "Generic configuration-change message", None,
         True, False, None, None, None),
    ]
    runs = [(t1 + timedelta(minutes=4), "core-sw1", "running", "event", 1, "admin"),
            (datetime(2026, 9, 12, 2, 0, tzinfo=timezone.utc), "lab-sw9", "running", "schedule", 0, None)]

    def daily(sql, p):
        if "FROM ncm_backup_runs r" in sql:
            return [(_riyadh_midnight_epoch(2026, 9, 29), 2), (_riyadh_midnight_epoch(2026, 9, 12), 1)]
        return [(_riyadh_midnight_epoch(2026, 9, 29), 4)]
    return FakeDB([
        ("LEFT JOIN ncm_jobs j", lambda sql, p: events[:p["lim"]]),
        ("string_agg(DISTINCT x.reported_user", runs),
        ("EXTRACT(EPOCH", daily),
        ("count(*) FILTER (WHERE e.source = 'api')", [(4, 3, 2, 1, 1, 2, 1, 1)]),
        ("count(*) FILTER (WHERE NOT", [(3, 1, 2, 1)]),
    ])


def test_change_audit(monkeypatch):
    db = _audit_db()
    sec = run(monkeypatch, db, "ncm_change_audit")
    assert kpi(sec, "Change notifications")["value"] == "4"
    assert kpi(sec, "Change notifications")["subtitle"] == "syslog 2 · trap 1 · API 1"
    assert kpi(sec, "Devices notifying")["value"] == "3"
    assert kpi(sec, "Confirmed by backup")["value"] == "1 of 4"
    assert kpi(sec, "Captured changes")["value"] == "3"
    assert kpi(sec, "Captured changes")["subtitle"] == "on 2 device(s)"
    assert kpi(sec, "Changes without a notification")["value"] == "1"
    assert kpi(sec, "With a reported user")["value"] == "2 of 4"

    ev = table(sec, "Change notifications")
    assert ev["headers"][4] == "Reported user (unverified)"
    first, trap, api, blocked = ev["rows"]
    assert first[0] == "Sep 29 13:00" and first[2] == "Syslog"     # Riyadh time
    assert first[4:] == ["admin", "Change captured", "Sep 29 13:04"]
    assert trap[2] == "SNMP trap" and trap[4] == "—" and trap[5] == "No difference found"
    assert api[5] == "Awaiting backup" and api[6] == "—"
    assert blocked[5] == "Not captured: device not ready"
    runs = table(sec, "Captured configuration changes")
    assert runs["rows"][0] == ["Sep 29 13:04", "core-sw1", "Running", "Change event", "1", "admin"]
    assert runs["rows"][1][3:] == ["Scheduled", "0", "—"]

    chart = sec["charts"][0]["series"]
    assert chart["kind"] == "bars" and [s["name"] for s in chart["series"]] == [
        "Notifications", "Captured changes"]
    notif, captured = (s["points"] for s in chart["series"])
    assert len(notif) == 30                                          # every Riyadh day of September
    sep29 = datetime.fromtimestamp(_riyadh_midnight_epoch(2026, 9, 29), timezone.utc)
    by_t = {p["t"]: p["v"] for p in captured}
    assert by_t[sep29.isoformat().replace("+00:00", "Z")] == 2
    assert sum(p["v"] for p in notif) == 4 and sum(p["v"] for p in captured) == 3

    assert any("not a verified identity" in n for n in sec["notes"])
    assert any("1 notification(s) are still waiting" in n for n in sec["notes"])
    assert any("1 first capture(s)" in n for n in sec["notes"])
    assert any("Configuration text is never included" in n for n in sec["notes"])
    # The period and organisation timezone reach every query.
    for _sql, params in db.calls:
        assert params["f"] == FRM and params["t"] == TO and params["org_tz"] == RIYADH


def test_change_audit_source_option_and_filter(monkeypatch):
    db = _audit_db()
    sec = run(monkeypatch, db, {"id": "ncm_change_audit", "options": {"source": "syslog", "top_n": 2}})
    ev_sql, params = db.sql_with("LEFT JOIN ncm_jobs j")[0]
    assert "e.source = :source" in ev_sql and params["source"] == "syslog" and params["lim"] == 2
    assert kpi(sec, "Change notifications")["subtitle"] == "from syslog"
    assert "top 2 of 4" in table(sec, "Change notifications")["title"]
    assert any("whatever the notification source" in n for n in sec["notes"])

    db = _audit_db()
    run(monkeypatch, db, "ncm_change_audit", device_ids=[A, B])
    assert all(p["dev_ids"] == [A, B] and "d.id::text = ANY(:dev_ids)" in s for s, p in db.calls)


def test_change_audit_empty_period(monkeypatch):
    db = FakeDB([("count(*) FILTER (WHERE NOT", [(0, 2, 0, 0)])])
    sec = run(monkeypatch, db, "ncm_change_audit")
    assert sec["notes"][0] == "No configuration-change notifications or captured changes in this period."
    assert "2 first capture(s)" in sec["notes"][1]
    assert not sec["charts"]


# ── ncm_restore_readiness ──────────────────────────────────────────────────

def _restore_db(now):
    return FakeDB([("FROM device_ncm n JOIN devices d", [
        ("core-sw1", "passed", now - timedelta(days=30), "lab_device", "alice",
         now - timedelta(days=40), "running", 0),
        ("core-sw2", "passed", now - timedelta(days=400), "vendor_tool", "bob",
         now - timedelta(days=410), "running", 3),
        ("edge-rtr1", "partial", now - timedelta(days=10), "isolated_replica", "carol",
         now - timedelta(days=12), "startup", 1),
        ("fw1", "failed", now - timedelta(days=5), "production_rollback", "dave",
         now - timedelta(days=6), "running", 0),
        ("sw9", None, None, None, None, None, None, None),
    ])])


def test_restore_readiness(monkeypatch):
    now = utcnow()
    db = _restore_db(now)
    sec = run(monkeypatch, db, "ncm_restore_readiness")
    assert "WHERE n.enabled" in db.calls[0][0]
    assert kpi(sec, "Restore-tested and current")["value"] == "1 of 5"
    assert kpi(sec, "Restore-tested and current")["subtitle"] == "20.0% passed within 180 days"
    assert kpi(sec, "Never tested")["value"] == "1"
    assert kpi(sec, "Passed over 180 days ago")["value"] == "1"
    assert kpi(sec, "Partial or failed")["value"] == "2"
    assert kpi(sec, "Changed since tested version")["value"] == "2"
    t = table(sec, "Restore evidence by device")
    assert [r[0] for r in t["rows"]] == ["fw1", "edge-rtr1", "sw9", "core-sw2", "core-sw1"]
    assert [r[-1] for r in t["rows"]] == ["failed", "warning", "untested", "stale", "ok"]
    ok = t["rows"][-1]
    assert ok[1:5] == ["Passed", riyadh_date(now - timedelta(days=30)), "30 d", "Lab device"]
    assert ok[5] == f"Running · {riyadh_short(now - timedelta(days=40))}"
    assert t["rows"][2][1:] == ["Never tested", "—", "—", "—", "—", "—", "—", "untested"]
    assert any("Only a recorded restore test demonstrates recovery" in n for n in sec["notes"])


def test_restore_readiness_options_and_filter(monkeypatch):
    now = utcnow()
    sec = run(monkeypatch, _restore_db(now),
              {"id": "ncm_restore_readiness", "options": {"max_age_days": 500, "top_n": 2}})
    assert kpi(sec, "Restore-tested and current")["value"] == "2 of 5"
    t = table(sec, "Restore evidence by device")
    assert len(t["rows"]) == 2 and "top 2 of 5" in t["title"]

    db = _restore_db(now)
    run(monkeypatch, db, "ncm_restore_readiness", device_ids=[A])
    assert db.calls[0][1]["dev_ids"] == [A] and "d.id::text = ANY(:dev_ids)" in db.calls[0][0]

    sec = run(monkeypatch, FakeDB(), "ncm_restore_readiness")
    assert sec["notes"][0] == "No devices in scope have an enabled configuration-backup enrollment."


# ── ncm_backup_incidents ───────────────────────────────────────────────────

def _incidents_db():
    o1 = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)
    rows = [
        ("edge-rtr1", datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc), None, 4, "unreachable",
         "SSH unreachable: verify the management address, port, routing and access rules."),
        ("fw1", datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc),
         datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc), 2, "weird_new_cause", "Collection failed"),
        ("core-sw1", o1, o1 + timedelta(hours=2, minutes=30), 1, "authentication",
         "Authentication rejected: verify the dedicated account"),
    ]
    return FakeDB([
        ("percentile_cont(0.5)", [(3, 2, 2, 1, 9000.0, 9000.0, 3, 7)]),
        ("i.opened_at >= :pf", [(1,)]),
        ("GROUP BY 1 ORDER BY 2 DESC, 1", [("authentication", 1, 1), ("unreachable", 1, 1),
                                           ("weird_new_cause", 1, 1)]),
        ("LIMIT :lim", rows),
    ])


def test_backup_incidents(monkeypatch):
    db = _incidents_db()
    sec = run(monkeypatch, db, "ncm_backup_incidents")
    assert kpi(sec, "Incidents in period")["value"] == "3"
    assert kpi(sec, "Opened in period")["value"] == "2"
    assert kpi(sec, "Opened in period")["subtitle"] == "▲ 1 vs previous period"
    assert kpi(sec, "Open at period end")["value"] == "2"
    assert kpi(sec, "Recovered in period")["value"] == "1"
    assert kpi(sec, "Mean time to recovery")["value"] == "2 h 30 min"
    assert kpi(sec, "Mean time to recovery")["subtitle"] == "median 2 h 30 min"
    assert kpi(sec, "Failed backup jobs")["value"] == "7"
    prior = db.sql_with("i.opened_at >= :pf")[0][1]
    assert prior["pf"] == FRM - (TO - FRM) and prior["pt"] == FRM

    causes = table(sec, "Causes and recommended actions")
    assert [r[0] for r in causes["rows"]] == ["Authentication rejected", "SSH unreachable",
                                              "Weird new cause"]
    assert causes["rows"][0][3].startswith("Check the profile username")
    assert causes["rows"][2][3] == "—"
    inc = table(sec, "Incidents (open first)")
    edge, fw, core = inc["rows"]
    assert edge[1] == "Sep 20 09:00" and edge[2] == "Open" and edge[3].endswith("(ongoing)")
    assert edge[-1] == "open" and edge[4] == "4"
    # Recovered after the period ends: still open at period end.
    assert fw[2] == "Open" and fw[-1] == "open"
    assert core[2] == "Sep 10 13:30" and core[3] == "2 h 30 min" and core[-1] == "resolved"
    assert core[5] == "Authentication rejected"


def test_backup_incidents_filter_and_empty(monkeypatch):
    db = _incidents_db()
    run(monkeypatch, db, {"id": "ncm_backup_incidents", "options": {"top_n": 5}}, device_ids=[A])
    assert all(p["dev_ids"] == [A] for _s, p in db.calls)
    assert db.sql_with("LIMIT :lim")[0][1]["lim"] == 5

    sec = run(monkeypatch, FakeDB(), "ncm_backup_incidents")
    assert sec["notes"] == ["No configuration-backup failure incidents were open in this period."]
    assert kpi(sec, "Incidents")["value"] == "0"


# ── ncm_credential_hygiene ─────────────────────────────────────────────────

def _cred_db(now):
    profiles = [
        ("Branch key", "ssh", "key", now - timedelta(days=80), 90, False, False, True, 3, 2),
        ("Core RO", "ssh", "password", now - timedelta(days=100), 90, True, False, True, 8, 8),
        ("DC", "ssh", "password", now - timedelta(days=5), 90, False, False, True, 2, 2),
        ("Lab", "ssh", "password", None, 30, False, False, True, 0, 0),
        ("Legacy telnet", "telnet", "password", None, None, False, True, False, 1, 1),
    ]
    devices = [
        ("sw-auth", "10.0.0.5", "Core RO", "authentication", now - timedelta(hours=2),
         now - timedelta(days=3)),
        ("sw-noprof", "10.0.0.6", None, "no_profile", None, None),
        ("sw-nosecret", "10.0.0.7", "Legacy telnet", "no_secret", now - timedelta(hours=1), None),
    ]
    return FakeDB([
        ("x.problem IS NOT NULL", devices),
        ("FROM ncm_credentials c ORDER BY c.name", profiles),
        ("SELECT count(*) FROM device_ncm n WHERE n.enabled", [(12,)]),
    ])


def test_credential_hygiene(monkeypatch):
    now = utcnow()
    db = _cred_db(now)
    sec = run(monkeypatch, db, "ncm_credential_hygiene")
    assert kpi(sec, "Connection profiles")["value"] == "5"
    assert kpi(sec, "Connection profiles")["subtitle"] == "4 in use"
    assert kpi(sec, "Overdue for rotation")["value"] == "1"
    assert kpi(sec, "Due within 14 days")["value"] == "1"
    assert kpi(sec, "Rotation not tracked")["value"] == "2"
    assert kpi(sec, "Cleartext or legacy SSH")["value"] == "1"
    assert kpi(sec, "Devices with credential problems")["value"] == "3 of 12"

    prof = table(sec, "Connection profiles")
    assert [r[0] for r in prof["rows"]] == ["Core RO", "Branch key", "Lab", "Legacy telnet", "DC"]
    assert [r[-1] for r in prof["rows"]] == ["expired", "warning", "unknown", "untracked", "ok"]
    core, branch, lab, telnet, _dc = prof["rows"]
    assert core[1:3] == ["Password", "8"] and core[6] == "Default profile"
    assert core[3] == riyadh_date(now - timedelta(days=100))
    assert branch[1:3] == ["SSH key", "2 of 3"]
    assert branch[5] == riyadh_date(now - timedelta(days=80) + timedelta(days=90))
    assert lab[3] == "Not recorded" and lab[4] == "30 d"
    assert telnet[4] == "Not set"
    assert telnet[6] == "TELNET (cleartext); Legacy SSH algorithms; No secret saved"

    dev = table(sec, "Enrolled devices with credential problems")
    assert [r[0] for r in dev["rows"]] == ["sw-noprof", "sw-nosecret", "sw-auth"]
    assert dev["rows"][0][2:] == ["—", "No connection profile assigned", "—", "never"]
    assert dev["rows"][2][3] == "Authentication rejected by the device"
    assert any("SSH host keys are not verified" in n for n in sec["notes"])
    assert any("never included" in n for n in sec["notes"])
    # Fleet section: no device filter is bound, and none of the retired
    # host-key tables or secret columns are read as values.
    for sql, params in db.calls:
        assert "dev_ids" not in params
        assert "ncm_host_keys" not in sql and "ssh_auto_trust" not in sql
        assert "key_fingerprint" not in sql
        # The username is only tested for presence (profile configured).
        assert sql.count("c.username") == sql.count("COALESCE(c.username, '') <> ''")


def test_credential_hygiene_options_and_empty(monkeypatch):
    now = utcnow()
    sec = run(monkeypatch, _cred_db(now), {"id": "ncm_credential_hygiene",
                                          "options": {"due_days": 5, "top_n": 2}})
    assert kpi(sec, "Due within 5 days")["value"] == "0"
    prof = table(sec, "Connection profiles")
    assert len(prof["rows"]) == 2 and "top 2 of 5" in prof["title"]
    assert "top 2 of 3" in table(sec, "Enrolled devices")["title"]

    sec = run(monkeypatch, _cred_db(now), "ncm_credential_hygiene", device_ids=[A])
    assert any("device selection does not apply" in n for n in sec["notes"])

    sec = run(monkeypatch, FakeDB(), "ncm_credential_hygiene")
    assert sec["notes"][0].startswith("No NCCM connection profiles are configured")


# ── pack-wide rules and templates ──────────────────────────────────────────

def test_pack_registration():
    reg = rs.SECTION_REGISTRY
    assert all(reg[s]["fn"].__module__ == pack.__name__ for s in PACK_IDS)
    assert {s: reg[s]["scope"] for s in PACK_IDS} == {
        "firmware_inventory": "device", "hardware_serial_inventory": "device",
        "ncm_compliance_trend": "fleet", "ncm_compliance_exceptions": "device",
        "ncm_change_audit": "device", "ncm_restore_readiness": "device",
        "ncm_backup_incidents": "device", "ncm_credential_hygiene": "fleet"}
    assert reg["firmware_inventory"]["requires"] == ("devices.view",)
    assert reg["hardware_serial_inventory"]["requires"] == ("devices.view",)
    for sid in PACK_IDS[2:]:
        assert reg[sid]["requires"] == ("ncm.view",), sid
    snapshot = {s for s in PACK_IDS if reg[s]["semantics"] == "snapshot"}
    assert snapshot == {"firmware_inventory", "hardware_serial_inventory",
                        "ncm_compliance_exceptions", "ncm_restore_readiness",
                        "ncm_credential_hygiene"}
    for sid in snapshot:
        assert "as of report generation" in reg[sid]["description"], sid


@pytest.mark.parametrize("sid", PACK_IDS)
def test_no_query_reads_configuration_text_or_secrets(sid, monkeypatch):
    db = FakeDB()
    run(monkeypatch, db, sid)
    run(monkeypatch, db, sid, device_ids=[A])
    for sql, _p in db.calls:
        low = sql.lower()
        assert "content" not in low, sid                 # device_configs.content / content_enc
        # Secrets are only ever tested for presence, never selected.
        assert low.count("password_enc") == low.count("octet_length(c.password_enc)"), sid
        assert low.count("private_key_enc") == low.count("private_key_enc is not null"), sid
        assert "key_passphrase" not in low and "enable_password" not in low, sid
        assert "evidence" not in low and "snmp_community" not in low, sid


def test_templates():
    assert rt.validate_catalog() == []
    expected = {
        "ncm_backup_status_report": ("Configuration Backup Status", "configuration"),
        "ncm_change_audit_report": ("Configuration Change Audit", "configuration"),
        "ncm_compliance_trend_report": ("Compliance Trend", "configuration"),
        "ncm_compliance_exceptions_report": ("Compliance Exceptions", "security"),
        "ncm_restore_readiness_report": ("Restore Readiness", "configuration"),
        "firmware_os_versions_report": ("Firmware and OS Versions", "inventory"),
        "hardware_serial_report": ("Hardware Serial Number Inventory", "inventory"),
        "ncm_credential_hygiene_report": ("NCM Credential Hygiene", "security"),
        "monthly_config_compliance_review": ("Monthly Configuration Compliance Review",
                                             "configuration"),
    }
    for key, (title, category) in expected.items():
        t = rt.TEMPLATES[key]
        assert (t.title, t.category) == (title, category), key
        assert set(t.audience) <= set(rt.AUDIENCES) and t.icon, key
        assert rs.REPORT_PRESETS[key]["sections"] == t.section_ids
    review = rt.TEMPLATES["monthly_config_compliance_review"]
    assert review.featured and review.default_period == "previous_month"
    assert {"ncm_compliance_trend", "ncm_credential_hygiene", "ncm_restore_readiness",
            "ncm_protection"} <= set(review.section_ids)
    assert rt.TEMPLATES["ncm_backup_status_report"].section_ids[:2] == ["ncm_protection", "ncm_at_risk"]
    assert rt.filterable_for(rt.TEMPLATES["ncm_change_audit_report"].sections) == ["devices"]
    assert rt.TEMPLATES["ncm_credential_hygiene_report"].requires() == {"ncm.view"}
    firmware = rt.TEMPLATES["firmware_os_versions_report"].sections
    assert [e["options"].get("version") for e in firmware] == ["os", "firmware"]
