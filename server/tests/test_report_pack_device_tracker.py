"""Device Tracker (UDT) section pack: switch port capacity, endpoint activity,
rogue and watched endpoints, endpoint inventory, IP address reassignments,
user logins and VLANs.

Every test runs the real section through report_sections.build_sections with
a fake Postgres handle that answers canned rows keyed by the query's tag
comment (``/* udt_… */``) and records every query, so the assertions cover the
SQL contract (device filter, option effects, evidence rules) as well as the
output. ClickHouse is never queried by this pack."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services import report_sections as rs
from app.services import report_templates as rt
from app.services.branding import Branding
from app.services.report_packs import device_tracker as pack

SW1 = "11111111-1111-1111-1111-111111111111"
SW2 = "22222222-2222-2222-2222-222222222222"
RTR = "33333333-3333-3333-3333-333333333333"
RIYADH = Branding(timezone="Asia/Riyadh")
TZ = ZoneInfo("Asia/Riyadh")
UTC = timezone.utc


# ── fakes ───────────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeDB:
    def __init__(self, rules=None):
        self.rules = dict(rules or {})
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        params = dict(params or {})
        self.calls.append((sql, params))
        for tag, rows in self.rules.items():
            if f"/* {tag} */" in sql:
                return _Result(rows(params) if callable(rows) else rows)
        return _Result([])

    def begin_nested(self):
        return _Nested()

    def q(self, tag):
        found = [c for c in self.calls if f"/* {tag} */" in c[0]]
        assert found, f"query {tag} did not run; ran {[self._tag(c[0]) for c in self.calls]}"
        return found[-1]

    def ran(self, tag):
        return any(f"/* {tag} */" in c[0] for c in self.calls)

    @staticmethod
    def _tag(sql):
        i = sql.find("/* ")
        return sql[i + 3:sql.find(" */", i)] if i >= 0 else sql[:40]


class NoCH:
    def query(self, *a, **k):
        raise AssertionError("the device_tracker pack must not query ClickHouse")


def _now():
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def run(sid, monkeypatch, db, *, options=None, days=7, end_days_ago=0, device_ids=None,
        requester=None):
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: NoCH())
    to = _now() - timedelta(days=end_days_ago)
    frm = to - timedelta(days=days)
    filters = {"device_ids": list(device_ids)} if device_ids else None
    out = asyncio.run(rs.build_sections(db, [{"id": sid, "options": options or {}}], frm, to, filters,
                                        requester=requester, branding=RIYADH))
    assert len(out) == 1
    sec = out[0]
    assert not sec.get("error"), sec["notes"]
    for k in sec["kpis"]:
        assert isinstance(k["value"], str), k
    for t in sec["tables"]:
        assert len(t["styles"]) == len(t["headers"])
        for row in t["rows"]:
            assert len(row) == len(t["headers"]), (t["headers"], row)
            assert all(isinstance(c, str) for c in row), row
    return sec


def kpi(sec, label):
    for k in sec["kpis"]:
        if k["label"] == label:
            return k
    raise AssertionError(f"no KPI {label!r} in {[k['label'] for k in sec['kpis']]}")


def table(sec, title_start):
    for t in sec["tables"]:
        if (t.get("title") or "").startswith(title_start):
            return t
    raise AssertionError(f"no table {title_start!r} in {[t.get('title') for t in sec['tables']]}")


def col(t, header):
    i = t["headers"].index(header)
    return [r[i] for r in t["rows"]]


def local_midnight_epoch(days_before_today: int) -> int:
    """Epoch of the Asia/Riyadh midnight `days_before_today` days ago (what
    Postgres returns for date_trunc('day', ts AT TIME ZONE tz) AT TIME ZONE tz)."""
    local = datetime.now(UTC).astimezone(TZ) - timedelta(days=days_before_today)
    return int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


# ── registration and templates ──────────────────────────────────────────────

SECTIONS = {
    "udt_port_capacity": ("device", ("udt.view",), "window"),
    "udt_endpoint_activity": ("device", ("udt.view",), "window"),
    "udt_rogue_watch": ("device", ("udt.view",), "snapshot"),
    "udt_endpoint_inventory": ("fleet", ("udt.view",), "snapshot"),
    "udt_ip_reassignments": ("device", ("udt.view",), "window"),
    "udt_user_logins": ("fleet", ("udt.view", "udt.view_users"), "window"),
    "udt_vlan_summary": ("device", ("udt.view",), "snapshot"),
}
TEMPLATES = {
    "udt_switch_port_capacity": "capacity",
    "udt_endpoint_activity_report": "device_tracker",
    "udt_rogue_watched_endpoints": "security",
    "udt_endpoint_inventory_report": "device_tracker",
    "udt_ip_address_reassignments": "device_tracker",
    "udt_user_logins_report": "device_tracker",
    "udt_vlan_summary_report": "device_tracker",
    "udt_device_tracker_overview": "device_tracker",
}


def test_sections_are_registered_with_scope_permissions_and_semantics():
    for sid, (scope, requires, semantics) in SECTIONS.items():
        e = rs.SECTION_REGISTRY[sid]
        assert e["fn"].__module__ == pack.__name__
        assert e["category"] == "Device Tracker", sid
        assert (e["scope"], e["requires"], e["semantics"]) == (scope, requires, semantics), sid
    device_scoped = {s for s, v in SECTIONS.items() if v[0] == "device"}
    assert device_scoped <= rs.DEVICE_SCOPABLE_SECTIONS
    assert "udt_endpoint_inventory" not in rs.SCOPE_SAFE_SECTIONS
    assert "udt_user_logins" not in rs.SCOPE_SAFE_SECTIONS


def test_templates_are_registered_valid_and_permission_aware():
    assert rt.validate_catalog() == []
    for key, category in TEMPLATES.items():
        t = rt.TEMPLATES[key]
        assert t.category == category, key
        assert set(t.audience) <= set(rt.AUDIENCES)
        assert key in rs.REPORT_PRESETS
    assert "devices" in rt.catalog_entry(rt.TEMPLATES["udt_switch_port_capacity"])["filterable"]
    viewer = frozenset({"udt.view"})
    assert not rt.is_available(rt.TEMPLATES["udt_user_logins_report"], viewer)
    assert rt.is_available(rt.TEMPLATES["udt_user_logins_report"], frozenset({"udt.view", "udt.view_users"}))
    assert rt.is_available(rt.TEMPLATES["udt_device_tracker_overview"], viewer)
    assert rt.TEMPLATES["udt_user_logins_report"].requires() == {"udt.view", "udt.view_users"}


def test_user_logins_are_withheld_without_view_users(monkeypatch):
    async def perms(db, requester):
        return frozenset({"udt.view"})
    monkeypatch.setattr(rs, "requester_permissions", perms)
    db = FakeDB({"udt_logins_totals": [(5, 2, 2, 0, 1)]})
    sec = run("udt_user_logins", monkeypatch, db, requester=SimpleNamespace(role="viewer"))
    assert sec.get("restricted") is True
    assert "udt.view_users" in sec["notes"][0]
    assert not db.calls, "a restricted section must not read user logons"


# ── udt_port_capacity ───────────────────────────────────────────────────────

def _cap_rules(today: date):
    latest = [
        (SW1, "access-sw1", "Riyadh HQ", today, 48, 40, 25, 2),
        (SW2, "access-sw2", "—", today - timedelta(days=2), 24, 23, 20, 1),
        ("44444444-4444-4444-4444-444444444444", "lab-sw", "—", today, 0, 0, 0, 0),
    ]
    trend = [(today - timedelta(days=5), 72, 54, 40, 2), (today, 72, 63, 45, 2)]
    return {"udt_port_capacity_latest": latest, "udt_port_capacity_trend": trend}


def test_port_capacity_kpis_table_and_trend(monkeypatch):
    today = datetime.now(UTC).astimezone(TZ).date()
    db = FakeDB(_cap_rules(today))
    sec = run("udt_port_capacity", monkeypatch, db)
    assert sec["title"] == "Switch Port Capacity"
    assert kpi(sec, "Switches")["value"] == "3"
    assert kpi(sec, "Switches")["subtitle"] == "3 uplink ports"
    usage = kpi(sec, "Port usage")
    assert usage["value"] == "87.5%" and usage["accent"] == "warning"
    assert usage["subtitle"].startswith("▲ 12.50 pts since ")
    assert kpi(sec, "Used ports")["value"] == "63"
    assert kpi(sec, "Used ports")["subtitle"] == "of 72 ports"
    assert kpi(sec, "Free ports")["value"] == "9"
    assert kpi(sec, "Switches ≥ 80% used")["value"] == "2"

    t = table(sec, "Ports per switch")
    assert col(t, "Switch") == ["access-sw2", "access-sw1", "lab-sw"]      # highest % used first
    assert col(t, "% used") == ["95.8%", "83.3%", "—"]                      # no ports = not measured
    assert col(t, "Status") == ["warning", "warning", "unknown"]
    assert col(t, "Free") == ["1", "8", "0"]

    chart = sec["charts"][0]["series"]
    assert chart["kind"] == "line" and chart["unit"] == "%" and chart["y_domain"] == [0, 100]
    assert [s["name"] for s in chart["series"]] == ["Used (link up)", "Endpoint seen in 24 h"]
    values = [p["v"] for p in chart["points"]]
    assert values[-1] == 87.5 and None in values                          # unsnapshotted days are gaps
    # Days are organisation-calendar days: Riyadh midnight is 21:00 UTC.
    assert all(p["t"].endswith("T21:00:00Z") for p in chart["points"])
    assert any("without a snapshot for" in n for n in sec["notes"])


def test_port_capacity_window_days_follow_organisation_timezone(monkeypatch):
    db = FakeDB({})
    run("udt_port_capacity", monkeypatch, db)
    _sql, params = db.q("udt_port_capacity_latest")
    now_local = datetime.now(UTC).astimezone(TZ)
    assert params["d1"] == now_local.date()
    assert params["d0"] == (now_local - timedelta(days=7)).date()


def test_port_capacity_device_filter_options_and_empty_state(monkeypatch):
    today = datetime.now(UTC).astimezone(TZ).date()
    db = FakeDB(_cap_rules(today))
    sec = run("udt_port_capacity", monkeypatch, db, device_ids=[SW1, SW2],
              options={"top_n": 1, "sort": "free", "threshold": 90})
    for tag in ("udt_port_capacity_latest", "udt_port_capacity_trend"):
        sql, params = db.q(tag)
        assert "c.device_id::text = ANY(:dev_ids)" in sql and params["dev_ids"] == [SW1, SW2]
    t = table(sec, "Ports per switch")
    assert t["title"] == "Ports per switch — top 1 of 3 switches"
    assert col(t, "Switch") == ["access-sw2"]                               # fewest free ports first
    assert kpi(sec, "Switches ≥ 90% used")["value"] == "1"
    assert "Limited to the selected switches." in sec["notes"]

    empty = run("udt_port_capacity", monkeypatch, FakeDB({}))
    assert empty["tables"] == [] and empty["notes"][0].startswith("No switch port snapshots")


# ── udt_endpoint_activity ───────────────────────────────────────────────────

def _activity_rules():
    d2 = local_midnight_epoch(2)
    d1 = local_midnight_epoch(1)
    when = datetime(2026, 10, 5, 21, 30, tzinfo=UTC)                        # Oct 06 00:30 in Riyadh
    return d1, d2, {
        "udt_activity_totals": [
            ("new_endpoint", 0, 5, 3, 5, 2),
            ("endpoint_moved", 0, 4, 4, 1, 0),
            ("rogue_detected", 0, 1, 0, 1, 1),
            ("watch_seen", 0, 0, 0, 0, 0),
            (None, 1, 10, 7, 6, 2),
        ],
        "udt_activity_daily": [("new_endpoint", d2, 3), ("new_endpoint", d1, 2), ("endpoint_moved", d1, 4),
                               ("rogue_detected", d2, 1)],
        "udt_activity_by_switch": [
            (SW1, "access-sw1", 3, 4, 0, 0, 7, when, 3),
            (None, None, 0, 0, 1, 0, 1, when, 3),
            (SW2, "access-sw2", 2, 0, 0, 0, 2, when, 3),
        ],
        "udt_activity_moves": [("pc-01", "00:14:22:01:02:03", "Dell Inc.", False, 4, 1, when, "access-sw1",
                                None, 17, 1)],
        "udt_activity_new": [
            (None, "02:aa:bb:cc:dd:ee", None, "access_point", True, None, "access-sw1", "Gi1/0/2", 2, when, 5),
        ],
        "udt_activity_security": [
            ("cam-lobby", "00:99:99:00:00:04", None, False, "rogue_detected", 1, when, when, None, None, None, 1),
        ],
    }


def test_endpoint_activity_kpis_chart_and_tables(monkeypatch):
    d1, d2, rules = _activity_rules()
    db = FakeDB(rules)
    sec = run("udt_endpoint_activity", monkeypatch, db)
    assert sec["title"] == "Endpoint Activity"
    new = kpi(sec, "New endpoints")
    assert new["value"] == "5" and new["subtitle"] == "▲ 2 vs previous period · 2 randomized MACs"
    assert kpi(sec, "Moves")["subtitle"] == "No change vs previous period"
    rogue = kpi(sec, "Rogue detections")
    assert rogue["accent"] == "danger" and rogue["subtitle"] == "▲ 1 vs previous period · 1 randomized MAC"
    assert kpi(sec, "Watch-list sightings")["value"] == "0"
    assert kpi(sec, "Endpoints involved")["value"] == "6"

    chart = sec["charts"][0]["series"]
    assert chart["kind"] == "stacked" and chart["unit"] == "events"
    assert [s["name"] for s in chart["series"]] == ["New endpoints", "Moves", "Rogue detections",
                                                    "Watch-list sightings"]
    totals = {p["t"]: p["v"] for p in chart["points"]}
    assert totals[iso(d2)] == 4 and totals[iso(d1)] == 6
    assert all(v is not None for v in totals.values())                     # recorded days with no events are 0

    t = table(sec, "Activity by switch")
    assert t["headers"] == ["Switch", "New endpoints", "Moves", "Rogue detections", "Watch-list sightings",
                            "Total", "Last event"]
    assert col(t, "Switch") == ["access-sw1", "No switch (location unknown)", "access-sw2"]
    assert col(t, "Last event")[0] == "Oct 06 00:30"                        # organisation timezone
    moves = table(sec, "Endpoints that moved most often")
    assert moves["rows"][0][5] == "access-sw1 · ifIndex 17"                 # reporter-local index with its switch
    # user_login events (personal data) are never part of this section.
    _sql, params = db.q("udt_activity_totals")
    assert params["types"] == list(pack.EVENT_TYPES) and "user_login" not in params["types"]
    assert params["org_tz"] == "Asia/Riyadh"


def test_endpoint_activity_focus_options(monkeypatch):
    _d1, _d2, rules = _activity_rules()
    db = FakeDB(rules)
    sec = run("udt_endpoint_activity", monkeypatch, db, options={"events": "new_endpoint", "top_n": 1})
    assert sec["title"] == "New Endpoints"
    assert [k["label"] for k in sec["kpis"]] == ["New endpoints"]
    assert db.q("udt_activity_totals")[1]["types"] == ["new_endpoint"]
    assert db.q("udt_activity_new")[1]["lim"] == 1
    assert sec["charts"][0]["series"]["kind"] == "bars"
    new = table(sec, "Newest 1 of 5 new endpoints")
    assert new["rows"][0] == ["02:aa:bb:cc:dd:ee", "02:aa:bb:cc:dd:ee", "Randomized MAC", "Access point", "—",
                              "access-sw1 · Gi1/0/2", "Oct 06 00:30"]
    assert table(sec, "Activity by switch")["headers"] == ["Switch", "New endpoints", "Last event"]
    assert not db.ran("udt_activity_moves")

    db = FakeDB(rules)
    sec = run("udt_endpoint_activity", monkeypatch, db, options={"events": "security"})
    assert sec["title"] == "Rogue and Watch-List Activity"
    assert db.q("udt_activity_totals")[1]["types"] == ["rogue_detected", "watch_seen"]
    sec_t = table(sec, "Rogue and watch-list events by endpoint")
    assert sec_t["headers"] == ["Endpoint", "MAC address", "Event", "Events", "First", "Last", "Last location"]
    assert sec_t["rows"][0][:3] == ["cam-lobby", "00:99:99:00:00:04", "Rogue detected"]
    assert sec_t["rows"][0][-1] == "Not located"


def test_endpoint_activity_device_filter_and_empty(monkeypatch):
    _d1, _d2, rules = _activity_rules()
    db = FakeDB(rules)
    sec = run("udt_endpoint_activity", monkeypatch, db, device_ids=[SW1])
    for tag in ("udt_activity_totals", "udt_activity_daily", "udt_activity_by_switch", "udt_activity_moves"):
        sql, params = db.q(tag)
        assert "ev.device_id::text = ANY(:dev_ids)" in sql and params["dev_ids"] == [SW1], tag
    assert any(n.startswith("Limited to activity reported by the selected switches") for n in sec["notes"])

    empty = run("udt_endpoint_activity", monkeypatch, FakeDB({}))
    assert empty["notes"][0] == "No endpoint activity in this period."
    assert kpi(empty, "New endpoints")["value"] == "0"


def test_endpoint_activity_retention(monkeypatch):
    _d1, _d2, rules = _activity_rules()
    gone = run("udt_endpoint_activity", monkeypatch, FakeDB(rules), days=30, end_days_ago=120)
    assert all(k["value"] == "—" for k in gone["kpis"])                     # pruned is not measured, not 0
    assert "more than 90 days ago" in gone["notes"][0]
    assert gone["tables"] == [] and gone["charts"] == []

    long = run("udt_endpoint_activity", monkeypatch, FakeDB(rules), days=92)
    assert any("kept for 90 days" in n for n in long["notes"])
    points = long["charts"][0]["series"]["points"]
    assert points[0]["v"] is None and points[-1]["v"] is not None          # pruned days are gaps
    assert "past the 90-day retention" in kpi(long, "New endpoints")["subtitle"]


# ── udt_rogue_watch ─────────────────────────────────────────────────────────

def _rogue_rules(allow=2):
    seen = datetime(2026, 10, 5, 21, 30, tzinfo=UTC)
    return {
        "udt_rules_enabled": [("allow", allow), ("watch", 1)] if allow else [("watch", 1)],
        # rogue, rogue_rand, watched, watched_rand, rogue_on, rogue_rand_on, watched_on,
        # watched_rand_on, listable, randomized
        "udt_rogue_watch_counts": [(3, 7, 1, 0, 2, 5, 0, 0, 4, 7)],
        "udt_rogue_watch_list": [
            ("cam-lobby", "00:99:99:00:00:04", None, "10.0.0.5", True, False, False, seen, seen, True,
             "access-sw2", 3, "Fa0/3", None, True),
            ("printer-2f", "00:1b:a9:00:00:01", "Brother", "10.0.0.30", True, True, False, seen, seen, False,
             "access-sw2", 3, None, 10, False),
            (None, "00:11:11:11:11:11", "Acme", None, True, False, False, seen, seen, False,
             None, None, None, None, None),
        ],
    }


def test_rogue_watch_kpis_and_table(monkeypatch):
    db = FakeDB(_rogue_rules())
    sec = run("udt_rogue_watch", monkeypatch, db)
    assert sec["title"] == "Rogue and Watched Endpoints"
    rogue = kpi(sec, "Rogue endpoints")
    assert rogue["value"] == "3" and rogue["accent"] == "danger" and rogue["subtitle"] == "2 online now"
    assert kpi(sec, "Randomized rogue")["value"] == "7"
    assert kpi(sec, "Randomized rogue")["subtitle"] == "5 online now"
    assert kpi(sec, "Watched endpoints")["value"] == "1"
    assert kpi(sec, "Allow-list rules")["value"] == "2"
    t = table(sec, "Endpoints")
    assert col(t, "Flag") == ["Rogue", "Rogue, watched", "Rogue"]
    assert col(t, "Location") == ["access-sw2 · Fa0/3", "access-sw2 · ifIndex 3 · VLAN 10 (last known)",
                                  "Not located"]
    assert col(t, "Presence") == ["active", "inactive", "inactive"]
    assert col(t, "Endpoint") == ["cam-lobby", "printer-2f", "Acme"]       # vendor when there is no name
    assert col(t, "IP address")[2] == "—"
    assert col(t, "Last seen")[0] == "Oct 06 00:30"
    assert any(n.startswith("7 randomized (private) MAC addresses counted above but not listed") for n in sec["notes"])
    sql, params = db.q("udt_rogue_watch_list")
    assert "AND NOT e.is_randomized" in sql and "LEFT JOIN LATERAL" in sql
    # No user identities leak into a udt.view section.
    assert not any("user_name" in s for s, _p in db.calls)


def test_rogue_watch_options_device_filter_and_no_allow_list(monkeypatch):
    db = FakeDB(_rogue_rules())
    sec = run("udt_rogue_watch", monkeypatch, db, device_ids=[SW2],
              options={"show": "rogue", "include_randomized": True, "top_n": 2})
    assert sec["title"] == "Rogue Endpoints"
    assert [k["label"] for k in sec["kpis"]] == ["Rogue endpoints", "Randomized rogue", "Allow-list rules"]
    counts_sql, params = db.q("udt_rogue_watch_counts")
    assert "EXISTS (SELECT 1 FROM udt_endpoint_locations l WHERE l.endpoint_id = e.id AND " \
           "l.device_id::text = ANY(:dev_ids))" in counts_sql and params["dev_ids"] == [SW2]
    list_sql, params = db.q("udt_rogue_watch_list")
    assert "\n        JOIN LATERAL" in list_sql and "LEFT JOIN LATERAL" not in list_sql
    assert "l.device_id::text = ANY(:dev_ids)" in list_sql and params["lim"] == 2
    assert "WHERE NOT e.ignored AND e.authorized = FALSE\n" in list_sql
    assert "NOT e.is_randomized\n" not in list_sql
    assert table(sec, "Endpoints")["title"] == "Endpoints — top 3 of 4 endpoints"

    off = run("udt_rogue_watch", monkeypatch, FakeDB(_rogue_rules(allow=0)))
    assert kpi(off, "Rogue endpoints")["subtitle"] == "Rogue detection is off"
    assert any(n.startswith("No allow-list rule is enabled") for n in off["notes"])

    none = run("udt_rogue_watch", monkeypatch, FakeDB({}))
    assert none["tables"] == [] and "No rogue or watched endpoints right now." in none["notes"]


# ── udt_endpoint_inventory ──────────────────────────────────────────────────

INV_TYPES = [("workstation", 50, 0, 30, 2), ("phone", 30, 24, 20, 0), ("unknown", 15, 1, 3, 6),
             ("door-readers", 5, 0, 5, 0)]
INV_VENDORS = [("Dell Inc.", 40, "workstation", 25, 5), ("Apple, Inc.", 10, "phone", 8, 5)]


def test_endpoint_inventory_kpis_charts_and_tables(monkeypatch):
    db = FakeDB({"udt_inventory_types": INV_TYPES, "udt_inventory_vendors": INV_VENDORS})
    sec = run("udt_endpoint_inventory", monkeypatch, db, options={"top_n": 2})
    assert kpi(sec, "Endpoints")["value"] == "100"
    assert kpi(sec, "Endpoints")["subtitle"] == "58 online now"
    assert kpi(sec, "Randomized MACs")["value"] == "25.0%"
    assert kpi(sec, "Randomized MACs")["subtitle"] == "25 private addresses"
    assert kpi(sec, "Unknown vendor")["value"] == "8.0%"
    assert kpi(sec, "Unclassified type")["value"] == "15.0%"
    assert kpi(sec, "Vendors")["value"] == "5"
    types = table(sec, "Endpoints by type")
    assert col(types, "Type") == ["Workstation", "Phone", "Unclassified", "Door readers"]
    assert col(types, "Share")[0] == "50.0%"
    vendors = table(sec, "Endpoints by vendor")
    assert vendors["title"] == "Endpoints by vendor — top 2 of 5 vendors"
    assert vendors["rows"][0] == ["Dell Inc.", "40", "40.0%", "Workstation", "25"]
    donut = sec["charts"][0]["series"]
    assert donut["kind"] == "donut" and donut["points"][0] == {"label": "Workstation", "value": 50}
    assert sec["charts"][1]["series"]["kind"] == "rank"
    sql, params = db.q("udt_inventory_vendors")
    assert "NOT e.is_randomized AND e.vendor IS NOT NULL" in sql and params == {"lim": 2, "days": 30}


def test_endpoint_inventory_seen_within_and_fleet_note(monkeypatch):
    db = FakeDB({"udt_inventory_types": INV_TYPES})
    sec = run("udt_endpoint_inventory", monkeypatch, db, options={"seen_within": "all"}, device_ids=[SW1])
    sql, params = db.q("udt_inventory_types")
    assert "make_interval" not in sql and "days" not in params
    assert "dev_ids" not in params                                         # fleet-wide by design
    assert sec["description"].startswith("Endpoints on record")
    assert any("device filter does not apply" in n for n in sec["notes"])

    db = FakeDB({})
    sec = run("udt_endpoint_inventory", monkeypatch, db, options={"seen_within": "7"})
    assert db.q("udt_inventory_types")[1]["days"] == 7
    assert sec["notes"] == ["No endpoints seen in the last 7 days."]


# ── udt_ip_reassignments ────────────────────────────────────────────────────

def _ip_rules(included=4):
    when = datetime(2026, 10, 5, 21, 30, tzinfo=UTC)
    return {
        # included, replaced, reused, endpoints, confirmed, unconfirmed, history_only,
        # concurrent_hidden, concurrent_listed
        "udt_ip_changes_summary": [(included, 2, 2, 3, 1, 1, 2, 5, 0)],
        # hostname, mac, prev_ip, ip, kind, prev_endpoint_mac, started, reporter, confirmed, bindings,
        # periods, vendor, randomized
        "udt_ip_changes_detail": [
            ("pc-01", "00:14:22:01:02:03", "10.0.0.5", "10.0.0.9", "replaced", None, when, "core-rtr1", True, 1, 1,
             "Dell Inc.", False),
            ("cam", "00:99:99:00:00:04", None, "10.0.0.5", None, "00:14:22:01:02:03", when, "core-rtr1", None, 2, 1,
             None, False),
            # Many observation periods but no per-reporter evidence: still "History only".
            (None, "00:aa:00:00:00:01", "10.0.0.7", "10.0.0.8", "replaced", None, when, None, None, 0, 6,
             "Acme", False),
            ("kiosk", "00:aa:00:00:00:02", "10.0.0.11", "10.0.0.12", "replaced", "00:aa:00:00:00:09", when,
             "core-rtr1", False, 0, 1, None, False),
        ],
        "udt_ip_shared_addresses": [
            ("10.0.0.5", 5, 2, ["00:01:00:00:00:01", "00:01:00:00:00:02", "00:01:00:00:00:03", "00:01:00:00:00:04"],
             when, 9, 1, 3),
        ],
    }


def test_ip_reassignments_kpis_evidence_and_tables(monkeypatch):
    db = FakeDB(_ip_rules())
    sec = run("udt_ip_reassignments", monkeypatch, db)
    assert kpi(sec, "Address changes")["value"] == "2"
    assert kpi(sec, "Addresses reused")["value"] == "2"
    assert kpi(sec, "Endpoints affected")["value"] == "3"
    conf = kpi(sec, "Confirmed")
    assert conf["value"] == "1" and conf["subtitle"] == "of 4 · 3 sightings in 24 h"
    assert kpi(sec, "Unconfirmed / history only")["value"] == "1 / 2"
    assert kpi(sec, "Shared addresses")["value"] == "3"

    t = table(sec, "Address changes, newest first")
    assert col(t, "Change") == ["Changed", "Reused", "Changed", "Changed, reused"]
    assert col(t, "Evidence") == ["Confirmed", "Unconfirmed", "History only", "History only"]
    assert col(t, "First seen") == ["Oct 06 00:30", "Oct 06 00:30", "Oct 06 00:30 (6 periods)", "Oct 06 00:30"]
    assert col(t, "Address") == ["10.0.0.5 → 10.0.0.9", "10.0.0.5", "10.0.0.7 → 10.0.0.8",
                                 "10.0.0.11 → 10.0.0.12"]
    assert col(t, "Endpoint") == ["pc-01", "cam", "Acme", "kiosk"]
    assert col(t, "Previous endpoint") == ["—", "00:14:22:01:02:03", "—", "00:aa:00:00:00:09"]
    assert col(t, "Reported by")[2] == "—"
    shared = table(sec, "Addresses reported for more than one endpoint")
    assert shared["rows"][0][2] == "00:01:00:00:00:01, 00:01:00:00:00:02 +3 more"
    assert shared["rows"][0][4] == "1"
    assert any("do not establish exclusive ownership" in n for n in sec["notes"])
    assert any(n.startswith("5 additional addresses reported alongside") for n in sec["notes"])

    sql, params = db.q("udt_ip_changes_summary")
    # The 3-in-24h rule comes from per-reporter evidence, never from period counts.
    assert "cardinality(x.recent_sightings) >= 3" in sql and "INTERVAL '24 hours'" in sql
    assert "FROM udt_ip_evidence x" in sql
    assert "family(h.ip) = 4" in sql and params["with_concurrent"] is False
    assert "dev_ids" not in params


def test_ip_reassignments_device_filter_family_and_empty(monkeypatch):
    db = FakeDB(_ip_rules())
    run("udt_ip_reassignments", monkeypatch, db, device_ids=[RTR],
        options={"family": "all", "include_concurrent": True, "top_n": 5})
    sql, params = db.q("udt_ip_changes_detail")
    for alias in ("h", "p", "o", "x"):
        assert f"{alias}.reporting_device_id::text = ANY(:dev_ids)" in sql, alias
    assert params["dev_ids"] == [RTR] and params["with_concurrent"] is True and params["lim"] == 5
    assert "family(h.ip) =" not in sql
    shared_sql, _p = db.q("udt_ip_shared_addresses")
    assert "o.reporting_device_id::text = ANY(:dev_ids)" in shared_sql
    assert "x.reporting_device_id::text = ANY(:dev_ids)" in shared_sql

    db = FakeDB({})
    sec = run("udt_ip_reassignments", monkeypatch, db, options={"family": "ipv6"})
    assert not db.ran("udt_ip_changes_detail")                              # nothing to list
    assert sec["tables"] == []
    assert sec["notes"][0] == "No IPv6 address changes or shared addresses in this period."
    assert "family(h.ip) = 6" in db.q("udt_ip_changes_summary")[0]


# ── udt_user_logins ─────────────────────────────────────────────────────────

def _login_rules():
    when = datetime(2026, 10, 5, 21, 30, tzinfo=UTC)
    return {
        "udt_logins_totals": [(40, 3, 2, 4, 2)],
        "udt_logins_daily": [(local_midnight_epoch(1), 25), (local_midnight_epoch(3), 15)],
        "udt_logins_by_pair": [
            ("alice", "CORP", "pc-01", "00:14:22:01:02:03", "10.0.0.9", "PC-01", 30, when, when, True, 4),
            ("carol", None, None, None, "10.9.9.9", "KIOSK-7", 4, when, when, False, 4),
        ],
        "udt_logins_by_user": [("alice", "CORP", 30, 2, 2, when, "pc-01", 3)],
        "udt_logins_by_endpoint": [("pc-01", "00:14:22:01:02:03", "10.0.0.9", 32, 2, when, "bob", "CORP", 2)],
    }


def test_user_logins_kpis_chart_and_pairs(monkeypatch):
    db = FakeDB(_login_rules())
    sec = run("udt_user_logins", monkeypatch, db, options={"top_n": 2})
    assert sec["title"] == "User Logins by Endpoint"
    assert kpi(sec, "Logon events")["value"] == "40"
    assert kpi(sec, "Logon events")["subtitle"] == "From 2 domain controllers"
    assert kpi(sec, "Users")["value"] == "3"
    assert kpi(sec, "Not matched")["value"] == "10.0%"
    bars = sec["charts"][0]["series"]
    assert bars["kind"] == "bars"
    by_t = {p["t"]: p["v"] for p in bars["points"]}
    assert by_t[iso(local_midnight_epoch(1))] == 25 and by_t[iso(local_midnight_epoch(2))] == 0
    t = table(sec, "Logons by user and endpoint")
    assert t["title"] == "Logons by user and endpoint — top 2 of 4 user/endpoint pairs"
    assert t["rows"][0] == ["CORP\\alice", "pc-01", "00:14:22:01:02:03", "10.0.0.9", "30", "Oct 06 00:30",
                            "Oct 06 00:30"]
    assert t["rows"][1][:3] == ["carol", "KIOSK-7 (not matched)", "—"]
    assert db.q("udt_logins_by_pair")[1]["lim"] == 2


def test_user_logins_group_by_and_summary_options(monkeypatch):
    db = FakeDB(_login_rules())
    sec = run("udt_user_logins", monkeypatch, db, options={"group_by": "user", "show_summary": False})
    assert sec["title"] == "Logins by User" and sec["kpis"] == [] and sec["charts"] == []
    assert not db.ran("udt_logins_daily") and not db.ran("udt_logins_by_pair")
    assert table(sec, "Logons by user")["rows"][0] == ["CORP\\alice", "30", "2", "2", "Oct 06 00:30", "pc-01"]

    db = FakeDB(_login_rules())
    sec = run("udt_user_logins", monkeypatch, db, options={"group_by": "endpoint"})
    t = table(sec, "Logons by endpoint, most users first")
    assert t["rows"][0] == ["pc-01", "00:14:22:01:02:03", "10.0.0.9", "2", "32", "CORP\\bob", "Oct 06 00:30"]

    gone = run("udt_user_logins", monkeypatch, FakeDB(_login_rules()), days=30, end_days_ago=120)
    assert gone["tables"] == [] and "kept for 90 days" in gone["notes"][0]
    empty = run("udt_user_logins", monkeypatch, FakeDB({}))
    assert empty["notes"][0].startswith("No user logons in this period.")


# ── udt_vlan_summary ────────────────────────────────────────────────────────

def _vlan_rules():
    polled = datetime(2026, 10, 5, 21, 30, tzinfo=UTC)
    return {
        "udt_vlan_totals": [(120, 4)],
        # dev_id, switch, vlan, name, names, switches, endpoints, randomized, ports, last_seen
        "udt_vlan_summary": [
            (None, None, 10, "Users", 2, 3, 90, 12, 60, polled),
            (None, None, 20, "Guest", 1, 2, 30, 25, 10, polled),
            (None, None, 99, None, 0, 1, 0, 0, 0, polled),
        ],
    }


def test_vlan_summary_kpis_table_and_options(monkeypatch):
    db = FakeDB(_vlan_rules())
    sec = run("udt_vlan_summary", monkeypatch, db)
    assert sec["title"] == "VLAN Summary"
    assert kpi(sec, "VLANs")["value"] == "3"
    assert kpi(sec, "VLANs in use")["value"] == "2"
    assert kpi(sec, "Endpoints on VLANs")["subtitle"] == "4 without VLAN information"
    assert kpi(sec, "VLANs without endpoints")["value"] == "1"
    assert kpi(sec, "Largest VLAN")["value"] == "VLAN 10 · Users"
    assert kpi(sec, "Name mismatches")["value"] == "1"
    t = table(sec, "Endpoints and ports per VLAN")
    assert col(t, "VLAN") == ["10", "20", "99"]
    assert col(t, "Name") == ["Users (+1 other name)", "Guest", "—"]
    assert col(t, "Share of endpoints") == ["75.0%", "25.0%", "0.0%"]
    sql, _p = db.q("udt_vlan_summary")
    assert "GROUP BY v.vlan_id" in sql and "FULL OUTER JOIN" in sql

    db = FakeDB(_vlan_rules())
    sec = run("udt_vlan_summary", monkeypatch, db, options={"include_empty": False, "top_n": 1})
    t = table(sec, "Endpoints and ports per VLAN")
    assert t["title"] == "Endpoints and ports per VLAN — top 1 of 2 VLANs"
    assert "1 VLAN without active endpoints not listed." in sec["notes"]


def test_vlan_summary_by_switch_and_device_filter(monkeypatch):
    polled = datetime(2026, 10, 5, 21, 30, tzinfo=UTC)
    db = FakeDB({"udt_vlan_totals": [(3, 0)],
                 "udt_vlan_summary": [(SW1, "access-sw1", 10, "Users", 1, 1, 2, 0, 2, polled),
                                      (SW2, "access-sw2", 10, "USERS", 1, 1, 1, 1, 1, polled)]})
    sec = run("udt_vlan_summary", monkeypatch, db, device_ids=[SW1, SW2], options={"breakdown": "switch"})
    assert sec["title"] == "VLANs by Switch"
    t = table(sec, "VLANs per switch")
    assert t["rows"][0] == ["access-sw1", "10", "Users", "2", "2", "0", "Oct 06 00:30"]
    assert kpi(sec, "VLANs")["subtitle"] == "On 2 switches"
    assert kpi(sec, "Name mismatches")["value"] == "1"
    for tag in ("udt_vlan_totals", "udt_vlan_summary"):
        sql, params = db.q(tag)
        assert params["dev_ids"] == [SW1, SW2], tag
    sql, _p = db.q("udt_vlan_summary")
    assert "v.device_id::text = ANY(:dev_ids)" in sql and "l.device_id::text = ANY(:dev_ids)" in sql
    assert "GROUP BY v.device_id, v.vlan_id" in sql and "u.device_id = d.device_id" in sql

    empty = run("udt_vlan_summary", monkeypatch, FakeDB({}))
    assert empty["notes"][0].startswith("No VLANs")


# ── helpers ─────────────────────────────────────────────────────────────────

def test_helpers():
    assert pack._count(1, "switch", "switches") == "1 switch"
    assert pack._count(2, "switch", "switches") == "2 switches"
    assert pack._share(1, 0) == "—"
    assert pack._count_change(5, None).startswith("Previous period is past")
    assert pack._count_change(3, 5) == "▼ 2 vs previous period"
    assert pack._type_label("access_point") == "Access point"
    assert pack._type_label("badge-readers") == "Badge readers"
    assert pack._port(None, 7) == "ifIndex 7" and pack._port(None, None) == "—"
    assert pack._user("bob", "CORP") == "CORP\\bob"
    assert pack._date_noon(date(2026, 10, 7)).hour == 12
