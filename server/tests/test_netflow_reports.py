"""NetFlow report sections (NF-04 report side, NF-25, FE-07).

Unit tests run everywhere. Live tests are opt-in (see test_netflow_alerts):
``NETFLOW_TEST_CLICKHOUSE`` for the sections over synthetic flows,
``ZENPLUS_NETFLOW_TEST_PG`` in addition for the executive summary, and
``ZENPLUS_NETFLOW_B8_URL`` + ``ZENPLUS_NETFLOW_B8_PG`` for the scoped-viewer
report acceptance against a running API.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.netflow import common as C
from app.api.v1.netflow.access import FlowVisibility
from app.services import netflow_report as nr
from app.services import report_sections as rsec

ADMIN = FlowVisibility(restricted=False, exporters=None, user_id=None, permissions=frozenset({"system.admin"}))
NO_FLOW = FlowVisibility(restricted=False, exporters=None, user_id=None, permissions=frozenset({"dashboard.view"}))


class Ctx(rsec.SectionCtx):
    """SectionCtx with a fixed visibility (no user lookup)."""

    def __init__(self, frm, to, vis, filters=None, db=None):
        super().__init__(db, frm, to, filters)
        self._vis = vis

    async def flow_visibility(self):
        return self._vis


# ── Pure helpers ─────────────────────────────────────────────────────────────

def test_whole_scope_denominators_and_remainder():
    rows = [("A", 50, 5), ("B", 30, 3), ("C", 10, 1)]
    out = nr.with_remainder(rows, 100, 2, "Other")
    assert out == [("A", 50, 5), ("B", 30, 3), ("Other", 20, None)]
    assert sum(r[1] for r in out) == 100                       # the table adds up to the total
    assert nr.with_remainder(rows, 90, 3) == rows              # nothing left over: no remainder row
    assert nr.with_remainder(rows, None, 2) == rows[:2]        # unknown total: no invented remainder
    assert nr.share(25, 100) == 25.0 and nr.share(1, 0) is None and nr.share(None, 5) is None


def test_change_is_unknown_without_a_comparable_prior():
    assert nr.change_pct(150, 100) == 50.0
    assert nr.change_pct(150, 0) is None and nr.change_pct(150, None) is None and nr.change_pct(None, 5) is None
    assert nr.fmt_change(None) == "—" and nr.fmt_change(0.01) == "±0%" and nr.fmt_change(-12.34) == "−12.3%"


def test_formatting_is_decimal_si_and_never_fabricates():
    assert nr.fmt_bytes(1_500_000) == "1.50 MB" and nr.fmt_bytes(None) == "—"
    assert nr.fmt_bps(0.4) == "<1 bps" and nr.fmt_bps(2_000_000_000) == "2.00 Gbps"
    assert nr.fmt_count(None) == "—" and nr.fmt_pct(None) == "—"
    assert nr.fmt_pct(0.01) == "<0.1%" and nr.fmt_pct(0) == "0.0%" and nr.fmt_pct(0.06) == "0.1%"


def test_recommendations_follow_the_evidence():
    cap_row = {"exporter": "10.0.0.1", "ifindex": 3, "device_hostname": "edge-1", "display_name": "WAN",
               "speed": {"in_bps": 1e9, "out_bps": 1e9},
               "headline": {"state": "critical", "direction": "out", "statistic": "p95",
                            "utilization_pct": 91.2, "headroom_bps": 8.8e7}}
    x = nr.ExecInputs(total_change_pct=120.0, top_growth=("Web", 300.0), capacity={"rows": [cap_row]},
                      silent_exporters=["edge-2 (10.0.0.2)"], low_coverage_exporters=[("10.0.0.3", 40.0)],
                      no_flag_exporters=["10.0.0.4"], unmapped_exporters=["10.0.0.5"],
                      findings={"by_severity": {"critical": 2, "warning": 5}},
                      retention_limited=True, raw_days=30)
    actions = nr.recommend(x)
    text = " | ".join(a for _, a, _ in actions)
    assert actions[0][0] == "High"
    assert "Plan capacity for edge-1 / WAN (out)" in text
    assert "Restore flow export from edge-2 (10.0.0.2)" in text
    assert "Review 2 high-severity detection findings" in text
    assert "Investigate collection gaps from 10.0.0.3" in text
    assert "Export TCP flags from 10.0.0.4" in text and "Map 10.0.0.5 to its device" in text
    why = dict((a, w) for _, a, w in actions)
    assert "p95 utilisation 91.2% of 1.00 Gbps" in why["Plan capacity for edge-1 / WAN (out)"]
    assert "led by Web (+300.0%)" in why["Confirm the change in traffic volume is expected"]
    # Priorities are ordered High, Medium, Low.
    order = [p for p, _, _ in actions]
    assert order == sorted(order, key=["High", "Medium", "Low"].index)
    assert nr.recommend(nr.ExecInputs()) == []


def test_report_scope_from_query_string():
    assert nr.scope_from_query("exporter=::ffff:10.0.0.1&dst_port=0") == {"exporter": "10.0.0.1", "dst_port": "0"}
    assert nr.scope_from_query("") == {}
    with pytest.raises(C.NetflowError):
        nr.scope_from_query("protocol=999")


def test_sections_without_netflow_permission_show_no_numbers():
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    ctx = Ctx(start, start + timedelta(days=1), NO_FLOW)
    for fn in (nr.sec_traffic_kpis, nr.sec_traffic_trend, nr.sec_traffic_protocols, nr.sec_traffic_ports,
               nr.sec_traffic_talkers, nr.sec_executive_summary):
        sec = asyncio.run(fn(ctx))
        assert not sec["kpis"] and not sec["tables"] and not sec["charts"], fn.__name__
        assert "netflow.view" in sec["notes"][0]


def test_invalid_or_foreign_scope_is_reported_not_ignored():
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    bad = Ctx(start, start + timedelta(days=1), ADMIN, filters={"netflow": {"protocol": "999"}})
    sec = asyncio.run(nr.sec_traffic_kpis(bad))
    assert not sec["kpis"] and "not valid" in sec["notes"][0]
    scoped = FlowVisibility(restricted=True, exporters=frozenset({"10.0.0.1"}), user_id=None,
                            permissions=frozenset({"netflow.view"}))
    foreign = Ctx(start, start + timedelta(days=1), scoped, filters={"netflow": {"exporter": "10.0.0.2"}})
    sec = asyncio.run(nr.sec_traffic_kpis(foreign))
    assert not sec["kpis"] and "cannot see" in sec["notes"][0]


def test_notable_findings_are_grouped_per_kind_and_host():
    t = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)

    def f(kind, src, sev, minutes, status="open", occ=1):
        return {"id": str(uuid.uuid4()), "kind": kind, "severity": sev, "confidence": "high", "status": status,
                "title": f"{kind} title", "src": src, "dst": None, "exporter": "10.0.0.1",
                "first_seen": t + timedelta(minutes=minutes - 5), "last_seen": t + timedelta(minutes=minutes),
                "occurrences": occ}

    items = [f("threat_intel", "10.20.3.39", "critical", 1, occ=2),
             f("threat_intel", "10.20.3.39", "warning", 9, status="acknowledged", occ=3),
             f("threat_intel", "10.20.3.39", "critical", 4),
             f("threat_intel", "10.20.1.31", "critical", 2),
             f("syn_scan", "10.20.3.39", "high", 30)]
    groups = nr.group_findings(items)
    assert len(groups) == 2                                     # same detection = one item
    top = groups[0]
    assert (top["kind"], top["findings"], top["hosts"]) == ("threat_intel", 4, ["10.20.3.39", "10.20.1.31"])
    assert top["severity"] == "critical" and top["occurrences"] == 7
    assert top["last_seen"] == t + timedelta(minutes=9) and top["status"] == "open"
    assert top["first_seen"] == t - timedelta(minutes=4)
    assert [g["severity"] for g in groups] == ["critical", "high"]   # worst first
    assert groups[1]["hosts"] == ["10.20.3.39"]


def test_orphaned_schedule_owner_withholds_netflow_data():
    vis = asyncio.run(nr.flow_visibility_for(None, nr.ORPHANED_OWNER))
    assert vis.restricted and vis.is_empty and not vis.can("netflow.view")
    assert asyncio.run(nr.flow_visibility_for(None, None)).exporters is None   # appliance-generated
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    ctx = rsec.SectionCtx(None, start, start + timedelta(days=1), None, requester=nr.ORPHANED_OWNER)
    for fn in (nr.sec_traffic_kpis, nr.sec_traffic_talkers, nr.sec_executive_summary):
        sec = asyncio.run(fn(ctx))
        assert not sec["kpis"] and not sec["tables"], fn.__name__
        assert "no longer exists" in sec["notes"][0]


def test_registry_and_preset():
    assert "netflow_executive_summary" in rsec.SECTION_REGISTRY
    preset = rsec.REPORT_PRESETS["netflow_executive"]
    assert preset["sections"][0] == "netflow_executive_summary"
    assert all(s in rsec.SECTION_REGISTRY for s in preset["sections"])


def test_catalog_marks_netflow_reports_scopable(client, as_admin):
    r = client.get("/api/v1/reports/catalog", headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    types = {t["key"]: t for t in r.json()["types"]}
    assert types["netflow_executive"]["filterable"] == ["netflow"]
    assert types["traffic"]["filterable"] == ["netflow"]
    assert types["availability"]["filterable"] == ["devices"]


def test_render_rejects_an_invalid_netflow_scope(client, as_admin):
    r = client.get("/api/v1/reports/render/traffic?format=json&netflow=protocol%3D999",
                   headers={"Authorization": "Bearer test"})
    assert r.status_code == 400 and "Invalid NetFlow scope" in r.json()["detail"]


def test_schedule_accepts_the_netflow_preset():
    from app.api.v1.report_schedules import ReportScheduleCreate

    s = ReportScheduleCreate(name="Weekly NetFlow", report_type="netflow_executive", period="last_7d",
                             filters={"netflow": {"exporter": "10.0.0.1"}})
    assert s.report_type == "netflow_executive"


# ── Live: sections over synthetic flows ─────────────────────────────────────

sys.path.insert(0, os.path.dirname(__file__))
from netflow_scratch_ch import flow_db, scratch_client  # noqa: E402

EXP_A, EXP_B = "10.87.0.1", "10.87.0.2"
_COLS = ["timestamp", "received_at", "exporter_ip", "src_addr", "dst_addr", "src_port", "dst_port", "protocol",
         "bytes", "packets", "fields_present", "first_switched_ms", "last_switched_ms"]


def _flow(t, exporter, src, dst, nbytes, dport=443, proto=6):
    ms = int(t.timestamp() * 1000)
    return [t, t, exporter, src, dst, 50000, dport, proto, nbytes, max(1, nbytes // 1000), (1 << 31) | 0x3F, ms, ms]


@pytest.fixture
def real_ch(monkeypatch):
    import threading

    client = scratch_client()
    per_thread = threading.local()

    def thread_client():
        if not hasattr(per_thread, "client"):
            per_thread.client = scratch_client()
        return per_thread.client

    monkeypatch.setattr(C, "get_ch_client", thread_client)
    C._retention_cache.update(value=None, at=0.0)
    with flow_db(client) as db:
        monkeypatch.setattr(C, "CH_DB", db)
        yield client, db
    C._retention_cache.update(value=None, at=0.0)


def _seed(client, db, start):
    """Two exporters, 2 h window: EXP_A carries 10 hosts to 8.8.8.8:53 and
    one big HTTPS flow; EXP_B a single host. Plus one record exactly at the
    window end (half-open: excluded) and prior-period traffic."""
    rows = []
    for h in range(2):
        t = start + timedelta(hours=h, minutes=7)
        for i in range(10):
            rows.append(_flow(t, EXP_A, f"10.1.0.{i + 1}", "8.8.8.8", 1000 * (i + 1), dport=53, proto=17))
        rows.append(_flow(t, EXP_A, "10.1.0.200", "93.184.216.34", 500_000))
        rows.append(_flow(t, EXP_B, "10.2.0.1", "1.1.1.1", 200_000))
    rows.append(_flow(start + timedelta(hours=2), EXP_A, "10.1.0.1", "8.8.8.8", 9_999_999))
    rows.append(_flow(start - timedelta(minutes=30), EXP_A, "10.1.0.200", "93.184.216.34", 600_000))
    client.insert(f"{db}.flow_records", rows, column_names=_COLS)
    return sum(1000 * (i + 1) for i in range(10)) * 2 + 500_000 * 2, 200_000 * 2


def _kpi(sec, label):
    return next(k["value"] for k in sec["kpis"] if k["label"] == label)


def test_live_sections_scope_denominators_and_half_open_window(real_ch):
    client, db = real_ch
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(hours=3)
    a_bytes, b_bytes = _seed(client, db, start)
    end = start + timedelta(hours=2)

    admin = Ctx(start, end, ADMIN)
    kpis = asyncio.run(nr.sec_traffic_kpis(admin))
    assert _kpi(kpis, "Total volume") == nr.fmt_bytes(a_bytes + b_bytes)
    assert _kpi(kpis, "Flow records") == "24"                   # the end-instant record is excluded

    # Requester visibility restricts every section (NF-04): only EXP_B.
    only_b = FlowVisibility(restricted=True, exporters=frozenset({EXP_B}), user_id=None,
                            permissions=frozenset({"netflow.view"}))
    scoped = Ctx(start, end, only_b)
    kpis_b = asyncio.run(nr.sec_traffic_kpis(scoped))
    assert _kpi(kpis_b, "Total volume") == nr.fmt_bytes(b_bytes)
    assert any("Restricted to the 1 flow exporter" in n for n in kpis_b["notes"])
    talkers_b = asyncio.run(nr.sec_traffic_talkers(scoped))
    hosts = {r[0] for t in talkers_b["tables"] for r in t["rows"]}
    assert hosts == {"10.2.0.1", "1.1.1.1"}                     # nothing from EXP_A leaks

    # The report's own NetFlow scope narrows further (admin, exporter A).
    scoped_a = Ctx(start, end, ADMIN, filters={"netflow": {"exporter": EXP_A}})
    assert _kpi(asyncio.run(nr.sec_traffic_kpis(scoped_a)), "Total volume") == nr.fmt_bytes(a_bytes)

    # NF-25: ranked tables are whole-scope shares with an explicit remainder.
    talkers = asyncio.run(nr.sec_traffic_talkers(admin))
    src = talkers["tables"][0]["rows"]
    assert len(src) == 11 and src[-1][0] == "All other sources"
    total = a_bytes + b_bytes
    assert src[0] == ["10.1.0.200", nr.fmt_bytes(1_000_000), nr.fmt_pct(1_000_000 / total * 100), "2"]
    shares = sum(float(r[2].rstrip("%")) for r in src)
    assert shares == pytest.approx(100.0, abs=0.6)
    apps = asyncio.run(nr.sec_traffic_ports(admin))
    app_rows = apps["tables"][0]["rows"]
    assert sum(float(r[2].rstrip("%")) for r in app_rows) == pytest.approx(100.0, abs=0.6)
    protos = asyncio.run(nr.sec_traffic_protocols(admin))
    by_proto = {r[0]: r[2] for r in protos["tables"][0]["rows"]}
    assert by_proto["UDP"] == nr.fmt_pct(110_000 / total * 100)


def test_live_trend_distinguishes_gaps_from_idle(real_ch):
    client, db = real_ch
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=3)
    _seed(client, db, start)
    ts, vals, _ = asyncio.run(nr.trend_series(C.Window(start=start, end=start + timedelta(hours=2), mode="fixed"),
                                              nr.FlowFilter(), ADMIN, "30 MINUTE"))
    assert len(ts) == 4
    # Records land at :07 of each hour -> the first half-hour of each hour has data,
    # the second half-hour had no deliveries at all: a gap (None), not zero.
    assert vals[0] and vals[2] and vals[1] is None and vals[3] is None
    ctx = Ctx(start, start + timedelta(hours=2), ADMIN)
    sec = asyncio.run(nr.sec_traffic_trend(ctx))
    pts = sec["charts"][0]["series"]["points"]
    _, fine, _ = asyncio.run(nr.trend_series(C.Window(start=start, end=start + timedelta(hours=2), mode="fixed"),
                                             nr.FlowFilter(), ADMIN, ctx.bucket()))
    assert len(pts) == 24 and [p["v"] is None for p in pts] == [v is None for v in fine]
    assert sum(p["v"] is not None for p in pts) == 2                # the two 5-minute slots with records
    assert any("collection gap" in n for n in sec["notes"])


PG_DSN = os.environ.get("ZENPLUS_NETFLOW_TEST_PG")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="set ZENPLUS_NETFLOW_TEST_PG")


@needs_pg
def test_live_executive_summary(real_ch):
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    client, db = real_ch
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(hours=3)
    a_bytes, b_bytes = _seed(client, db, start)

    async def run():
        engine = create_async_engine(PG_DSN)
        try:
            async with AsyncSession(engine) as pg:
                ctx = Ctx(start, start + timedelta(hours=2), ADMIN, db=pg)
                return await nr.sec_executive_summary(ctx)
        finally:
            await engine.dispose()

    sec = asyncio.run(run())
    assert _kpi(sec, "Traffic volume") == nr.fmt_bytes(a_bytes + b_bytes)
    kpi = next(k for k in sec["kpis"] if k["label"] == "Traffic volume")
    # The prior 2 h holds only 600 kB (partly) -> a comparable change is shown or honestly withheld.
    assert "vs previous period" in kpi["subtitle"] or "no comparable data" in kpi["subtitle"]
    titles = [t["title"] for t in sec["tables"]]
    assert titles[0] == "Recommended actions"
    assert {"Top applications", "Top talkers (sent + received)", "Coverage and data quality"} <= set(titles)
    talkers = next(t for t in sec["tables"] if t["title"].startswith("Top talkers"))
    assert talkers["rows"][0][0] in ("10.1.0.200", "93.184.216.34")
    dq = {r[0]: r for r in next(t for t in sec["tables"] if t["title"] == "Coverage and data quality")["rows"]}
    assert dq["Scope"][1] == "all flow traffic"
    assert dq["Talker shares"][1] == "sent + received"            # method, not a warning note
    cap_kpi = next(k for k in sec["kpis"] if k["label"] == "Capacity risks")
    if cap_kpi["value"] == "—":                                     # nothing assessable is never "0 risks"
        assert "speed" in cap_kpi["subtitle"] or "interface traffic" in cap_kpi["subtitle"] \
            or "unavailable" in cap_kpi["subtitle"]

    # Exporter B has no traffic in the previous period: "no data", never "previous 0 bps".
    async def run_b():
        engine = create_async_engine(PG_DSN)
        try:
            async with AsyncSession(engine) as pg:
                ctx = Ctx(start, start + timedelta(hours=2), ADMIN, db=pg, filters={"netflow": {"exporter": EXP_B}})
                return await nr.sec_executive_summary(ctx)
        finally:
            await engine.dispose()

    sec_b = asyncio.run(run_b())
    rate = next(k for k in sec_b["kpis"] if k["label"] == "Average rate")
    assert rate["subtitle"] == "previous period: no data"
    assert "no comparable data" in next(k for k in sec_b["kpis"] if k["label"] == "Traffic volume")["subtitle"]
    dq_b = {r[0]: r for r in next(t for t in sec_b["tables"] if t["title"] == "Coverage and data quality")["rows"]}
    assert dq_b["Previous period"][2] in ("no flow records in scope", "no retained data")
    assert dq_b["Scope"][1] == f"exporter {EXP_B}"


# ── Live: a scoped viewer's report equals the admin's scoped report ─────────

E2E_URL = os.environ.get("ZENPLUS_NETFLOW_B8_URL")
E2E_PG = os.environ.get("ZENPLUS_NETFLOW_B8_PG")
needs_e2e = pytest.mark.skipif(not (E2E_URL and E2E_PG), reason="set ZENPLUS_NETFLOW_B8_URL and ZENPLUS_NETFLOW_B8_PG")


@needs_e2e
def test_e2e_scoped_viewer_report(e2e_seed_reports):
    import requests

    from test_netflow_alerts import E2E_EXPORTER, E2E_VIEWER, _login

    viewer = _login(E2E_VIEWER)
    admin = _login(os.environ.get("ZENPLUS_ADMIN", "admin"), os.environ.get("ZENPLUS_ADMIN_PASS", "admin123"))
    # Far enough back that late flow records (long flows, exporter batching) have landed, so
    # two renders of the same fixed window see the same rows.
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=20)
    window = {"from": (end - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "format": "json"}

    def render(token, **extra):
        r = requests.get(f"{E2E_URL.rstrip('/')}/api/v1/reports/render/traffic",
                         params={**window, **extra}, headers={"Authorization": f"Bearer {token}"}, timeout=120)
        r.raise_for_status()
        return {s["id"]: s for s in r.json()["sections"]}

    mine = render(viewer)
    theirs = render(admin, netflow=f"exporter={E2E_EXPORTER}")
    everyone = render(admin)
    vol = lambda s: _kpi(s["traffic_kpis"], "Total volume")  # noqa: E731
    assert vol(mine) == vol(theirs) and vol(mine) != "—"
    assert vol(everyone) != vol(mine)
    assert any("Restricted to the" in n for n in mine["traffic_kpis"]["notes"])
    assert not any("could not be generated" in " ".join(s["notes"]) for s in mine.values())


@pytest.fixture(scope="module")
def e2e_seed_reports():
    from test_netflow_alerts import seeded_e2e

    with seeded_e2e() as did:
        yield did
