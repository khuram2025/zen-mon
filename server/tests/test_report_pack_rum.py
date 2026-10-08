"""Browser RUM section pack (report_packs/rum).

Every section runs through report_sections.build_sections against fakes:
Postgres and ClickHouse answer canned rows keyed by SQL substrings (rows
shaped as the drivers return them). The window is the seven local days before
today in Asia/Riyadh (UTC+3, no DST), so day buckets, chart timestamps and
table times prove the organisation timezone is honoured; a 30-day window
reaches past the 14-day raw-event retention and must read the 5-minute rollup."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import report_sections as rs
from app.services import report_templates as rt
from app.services.branding import Branding
from app.services.report_packs import rum
from tests.conftest import _NullTransaction

RIYADH = ZoneInfo("Asia/Riyadh")
BRAND = Branding(timezone="Asia/Riyadh")
_TODAY = datetime.now(RIYADH).replace(hour=0, minute=0, second=0, microsecond=0)
TO = _TODAY.astimezone(timezone.utc).replace(tzinfo=None)       # local midnight, naive UTC
FRM = TO - timedelta(days=7)
FRM30 = TO - timedelta(days=30)

SECTIONS = ("rum_vitals_trend", "rum_geography", "rum_releases", "rum_slow_resources",
            "rum_load_breakdown", "rum_funnel_conversion", "rum_user_actions", "rum_issue_status")
TEMPLATES = ("rum_core_web_vitals_trend", "rum_audience_by_country", "rum_release_experience",
             "rum_slow_failing_resources", "rum_page_load_breakdown", "rum_funnels",
             "rum_user_actions_long_tasks", "rum_javascript_issue_status", "rum_monthly_dx_review")


def local_day(i: int) -> datetime:
    """Local midnight (aware, Riyadh) of the i-th day of the 7-day window --
    what toStartOfInterval(..., INTERVAL 1 DAY, 'Asia/Riyadh') returns."""
    return (FRM.replace(tzinfo=timezone.utc) + timedelta(days=i)).astimezone(RIYADH)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc)


def vit(lcp=None, n_lcp=0, lg=0, lp=0, inp=None, n_inp=0, ig=0, ip=0, cls=None, n_cls=0, cg=0, cp=0,
        rated=None):
    """Raw per-vital columns: (p75, samples, good, poor, rated) x LCP/INP/CLS."""
    out = []
    for i, (p, n, g, b) in enumerate(((lcp, n_lcp, lg, lp), (inp, n_inp, ig, ip), (cls, n_cls, cg, cp))):
        r = n if rated is None else rated[i]
        out += [p if p is not None else float("nan"), n, g, b, r]
    return tuple(out)


# ── fakes ───────────────────────────────────────────────────────────────────

def _match(routes, sql, params):
    for key, rows in routes:
        keys = key if isinstance(key, tuple) else (key,)
        if all(k in sql for k in keys):
            return list(rows(sql, params) if callable(rows) else rows)
    return []


class Res:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeDB:
    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        sql, p = str(statement), dict(params or {})
        self.calls.append((sql, p))
        return Res(_match(self.routes, sql, p))

    def begin_nested(self):
        return _NullTransaction()

    async def commit(self):
        return None

    async def rollback(self):
        return None

    def call(self, needle: str) -> tuple[str, dict]:
        return next(c for c in self.calls if needle in c[0])


class _CHResult:
    def __init__(self, rows):
        self.result_rows = rows


class FakeCH:
    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None, **kw):
        p = dict(parameters or {})
        self.calls.append((sql, p))
        return _CHResult(_match(self.routes, sql, p))

    def call(self, *needles: str) -> tuple[str, dict]:
        return next(c for c in self.calls if all(n in c[0] for n in needles))

    def all(self, *needles: str) -> list[tuple[str, dict]]:
        return [c for c in self.calls if all(n in c[0] for n in needles)]


def run(monkeypatch, sid, db=None, ch=None, title=None, frm=FRM, to=TO, filters=None, **options):
    db = db or FakeDB()
    ch = ch or FakeCH()
    monkeypatch.setattr(rs.SectionCtx, "ch", lambda self: ch)
    out = asyncio.run(rs.build_sections(db, [{"id": sid, "title": title, "options": options}], frm, to,
                                        filters, requester=None, branding=BRAND))
    assert len(out) == 1
    sec = out[0]
    assert not sec.get("error"), sec["notes"]
    for t in sec["tables"]:
        assert len(t["styles"]) == len(t["headers"])
        for row in t["rows"]:
            assert len(row) == len(t["headers"]), (t["title"], row)
            assert all(isinstance(c, str) for c in row), row
    for k in sec["kpis"]:
        assert isinstance(k["value"], str), k
    assert 2 <= len(sec["kpis"]) <= 6 or not sec["kpis"], sec["kpis"]
    # Never read a visitor's address or (pseudonymised) identity into a report.
    for sql, _p in ch.calls + db.calls:
        assert "client_ip" not in sql and "user_id" not in sql, sql
    return sec


def kpi(sec, label):
    return next(k for k in sec["kpis"] if k["label"] == label)


# ── registry and catalog ────────────────────────────────────────────────────

def test_sections_are_fleet_scoped_apm_sections():
    for sid in SECTIONS:
        e = rs.SECTION_REGISTRY[sid]
        assert e["scope"] == "fleet", sid
        assert e["requires"] == ("apm.view",), sid
        assert e["fn"].__module__ == "app.services.report_packs.rum"
        assert e["semantics"] == "window"


def test_templates_are_registered_valid_and_filterable_by_application():
    assert rt.validate_catalog() == []
    for key in TEMPLATES:
        t = rt.TEMPLATES[key]
        assert t.category == "experience", key
        assert set(t.audience) <= set(rt.AUDIENCES)
        entry = rt.catalog_entry(t)
        assert "applications" in entry["filterable"], key
        assert entry["requires"] == ["apm.view"], key
    assert [k for k in TEMPLATES if rt.TEMPLATES[k].featured] == ["rum_core_web_vitals_trend"]
    assert rt.TEMPLATES["rum_core_web_vitals_trend"].title == "Core Web Vitals Trend"
    monthly = rt.TEMPLATES["rum_monthly_dx_review"]
    assert {"rum_audience", "rum_top_pages", "rum_segments", "rum_reliability"} <= set(monthly.section_ids)
    assert monthly.default_period == "previous_month" and monthly.frequency == "monthly"
    # The second country list hides the repeated tiles and chart.
    geo = [e for e in rt.catalog_entry(rt.TEMPLATES["rum_audience_by_country"])["entries"]]
    assert [e["options"]["show_summary"] for e in geo] == [True, False, False]


# ── helpers ─────────────────────────────────────────────────────────────────

def test_clean_url_strips_query_fragment_and_credentials():
    assert rum.clean_url("https://user:pw@CDN.example.com:8443/app.js?v=1&t=secret#x") == \
        "https://cdn.example.com:8443/app.js"
    assert rum.clean_url("https://[2001:db8::1]/a.css?x=1") == "https://[2001:db8::1]/a.css"
    assert rum.clean_url("/static/img.png?size=2") == "/static/img.png"
    assert rum.clean_url("data:image/png;base64,AAAA") == "data: URI (inline)"
    assert rum.clean_url("blob:https://x/1") == "blob: URL"
    assert rum.clean_url("") == "—"
    long = "https://cdn.example.com/" + "a" * 200 + "/bundle.js?token=abc"
    out = rum.clean_url(long)
    assert len(out) == 96 and out.endswith("/bundle.js") and "…" in out and "token" not in out


def test_country_labels():
    assert rum.country_label("sa") == "Saudi Arabia (SA)"
    assert rum.country_label("") == "Unknown"
    assert rum.country_label(None) == "Unknown"
    assert rum.country_label("XX") == "XX"


# ── rum_vitals_trend ────────────────────────────────────────────────────────

NOW_VITALS = (400,) + vit(2400, 300, 240, 15, 180, 250, 200, 5, 0.05, 280, 260, 2)
BEFORE_VITALS = (350,) + vit(2700, 200, 150, 20, 150, 180, 160, 3)


def _overall(sql, p):
    return [NOW_VITALS] if p["f"] == FRM else [BEFORE_VITALS]


def vitals_ch(trend_rows=None):
    trend_rows = trend_rows if trend_rows is not None else [
        (local_day(0), 100) + vit(2400, 100, 80, 5, 180, 90, 80, 2, 0.05, 95, 90, 1),
        (local_day(1), 120) + vit(2600, 110, 70, 10, 190, 100, 85, 3, 0.06, 100, 92, 2),
        (local_day(3), 90) + vit(2500, 90, 75, 4),            # day 2 missing, no INP / CLS
    ]
    # The previous period may start past raw retention (then the rollup answers).
    return FakeCH([("GROUP BY b", trend_rows), ("countIf(started = 1)", _overall),
                   ("uniqCombined64Merge(views)", _overall)])


def test_vitals_trend_raw_daily_kpis_bands_and_gaps(monkeypatch):
    ch = vitals_ch()
    sec = run(monkeypatch, "rum_vitals_trend", ch=ch)
    lcp = kpi(sec, "LCP (p75)")
    assert lcp["value"] == "2.40 s" and lcp["accent"] == "success"
    assert lcp["subtitle"] == "Good · ▼ 300 ms vs previous period"
    inp = kpi(sec, "INP (p75)")
    assert inp["value"] == "180 ms" and inp["subtitle"] == "Good · ▲ 30 ms vs previous period"
    cls = kpi(sec, "CLS (p75)")
    assert cls["value"] == "0.050" and cls["subtitle"] == "Good · No data for the previous period"
    good = kpi(sec, "Good LCP page views")
    assert good["value"] == "80.00%" and good["subtitle"] == "▲ 5.00 pts vs previous period"

    # One finalized, sampled sample per view, bucketed by local day.
    sql, params = ch.call("GROUP BY b")
    assert "final_view" in sql and "sampled = 1" in sql and "argMaxIf" in sql
    assert "INTERVAL 1 DAY" in sql and params["org_tz"] == "Asia/Riyadh"
    assert "apm_rum_metrics_5m" not in sql

    lcp_chart = next(c for c in sec["charts"] if c["title"] == "LCP p75 by day")["series"]
    assert lcp_chart["kind"] == "line" and lcp_chart["unit"] == "ms"
    assert [p["t"] for p in lcp_chart["points"]] == [iso(local_day(i)) for i in (0, 1, 2, 3)]
    assert [p["v"] for p in lcp_chart["points"]] == [2400, 2600, None, 2500]
    assert lcp_chart["points"][0]["t"] == iso(FRM)          # local midnight = 21:00Z the day before
    inp_chart = next(c for c in sec["charts"] if c["title"] == "INP p75 by day")["series"]
    assert inp_chart["points"][-1]["v"] is None              # no INP samples that day: a gap, not 0
    band = next(c for c in sec["charts"] if c["title"].startswith("LCP rating by day"))["series"]
    assert band["kind"] == "stacked" and band["y_domain"] == [0, 100]
    assert [s["name"] for s in band["series"]] == ["Good", "Needs improvement", "Poor"]
    assert [p["v"] for p in band["series"][0]["points"]] == [80.0, 63.64, 83.33]

    table = sec["tables"][0]
    assert table["rows"][0][0] == BRAND.fmt_date(local_day(3))        # newest first
    assert table["rows"][-1] == [BRAND.fmt_date(FRM), "100", "2.40 s", "180 ms", "0.050",
                                 "80.00%", "5.00%"]
    assert table["rows"][0][3:5] == ["—", "—"]
    assert any("GMT+3" in n for n in sec["notes"])


def test_vitals_trend_rating_vital_and_application_filter(monkeypatch):
    ch = vitals_ch()
    sec = run(monkeypatch, "rum_vitals_trend", ch=ch, filters={"application_id": "shop"},
              rating_vital="inp", interval="hour")
    assert kpi(sec, "Good INP page views")["value"] == "80.00%"
    assert any(c["title"].startswith("INP rating by hour") for c in sec["charts"])
    assert sec["tables"][0]["headers"][-2:] == ["Good INP", "Poor INP"]
    for sql, p in ch.all("apm_rum_events"):
        assert "application_id = %(app)s" in sql and p["app"] == "shop"
    assert "INTERVAL 1 HOUR" in ch.call("GROUP BY b")[0]


def test_vitals_trend_hourly_table_lists_the_newest_two_days(monkeypatch):
    start = FRM.replace(tzinfo=timezone.utc)
    rows = [(start + timedelta(hours=h), 10) + vit(2000 + h, 10, 9, 0) for h in range(60)]
    sec = run(monkeypatch, "rum_vitals_trend", ch=vitals_ch(rows), interval="hour")
    table = sec["tables"][0]
    assert table["title"] == "Core Web Vitals by hour — newest 48 of 60 hours"
    assert len(table["rows"]) == 48 and table["rows"][0][0] == BRAND.fmt_short(start + timedelta(hours=59))
    lcp = next(c for c in sec["charts"] if c["title"] == "LCP p75 by hour")["series"]
    assert len(lcp["points"]) == 60                          # the chart keeps every hour


def test_vitals_trend_reads_rollup_beyond_raw_retention(monkeypatch):
    rows = [(local_day(0), 500) + vit(2300, 100, 0, 0, rated=(0, 0, 0)),          # pre-band bucket
            (local_day(1), 600) + vit(2600, 100, 40, 5, 200, 80, 60, 4, 0.1, 90, 85, 1,
                                      rated=(50, 80, 90))]
    ch = FakeCH([("GROUP BY b", rows),
                 ("uniqCombined64Merge(views)", [(1100,) + vit(2500, 200, 40, 5, rated=(50, 0, 0))])])
    sec = run(monkeypatch, "rum_vitals_trend", ch=ch, frm=FRM30)
    sql, _p = ch.call("GROUP BY b")
    assert "apm_rum_metrics_5m" in sql and "quantileTDigestMerge(0.75)(lcp)" in sql
    assert "sum(lcp_rated)" in sql
    assert not ch.all("final_view")
    # Shares divide by rated samples: 40 good of 50 rated (not of 100 samples).
    assert kpi(sec, "Good LCP page views")["value"] == "80.00%"
    assert sec["tables"][0]["rows"][-1][-2:] == ["—", "—"]           # pre-band bucket: p75 only
    assert any("5-minute RUM aggregates" in n for n in sec["notes"])
    assert any("before rating counters existed" in n for n in sec["notes"])


def test_vitals_trend_honours_configured_raw_retention(monkeypatch):
    ch = vitals_ch()
    ch.routes.insert(0, ("system.tables", lambda sql, p: [
        ("ENGINE = MergeTree TTL toDateTime(timestamp) + toIntervalDay(45)",)]
        if p.get("ttl_table") == "apm_rum_events" else []))
    run(monkeypatch, "rum_vitals_trend", ch=ch, frm=FRM30)
    assert "final_view" in ch.call("GROUP BY b")[0]               # 30 days < 45-day raw TTL


def test_vitals_trend_empty_state(monkeypatch):
    sec = run(monkeypatch, "rum_vitals_trend", ch=vitals_ch([]))
    assert not sec["kpis"] and not sec["charts"] and not sec["tables"]
    assert sec["notes"][0].startswith("No Core Web Vitals were collected")


# ── rum_geography ───────────────────────────────────────────────────────────

GEO_ROWS = [("SA", 120, 300, 6), ("AE", 50, 90, 10), ("", 30, 40, 0), ("XX", 3, 3, 3)]
GEO_VITALS = [("SA",) + vit(2300, 250, 200, 10, 150, 200, 180, 5, 0.08, 240, 200, 10),
              ("AE",) + vit(4200, 80, 20, 50, 320, 70, 30, 10, 0.2, 75, 40, 10)]


def geo_ch():
    return FakeCH([("GROUP BY country", GEO_ROWS), ("GROUP BY dim_value", GEO_VITALS),
                   ("uniqExactIf((application_id, env, view_id)", [(200, 19, 433)])])


def test_geography_by_sessions(monkeypatch):
    ch = geo_ch()
    sec = run(monkeypatch, "rum_geography", ch=ch)
    assert sec["title"] == "Audience by Country"
    assert kpi(sec, "Countries")["value"] == "3"
    assert kpi(sec, "Sessions")["value"] == "200"
    assert kpi(sec, "Sessions")["subtitle"] == "9.50% had a JavaScript error"
    assert kpi(sec, "Top country")["value"] == "Saudi Arabia (SA)"
    assert kpi(sec, "Top country")["subtitle"] == "60.00% of sessions"
    assert kpi(sec, "Location unknown")["value"] == "15.00%"
    rows = sec["tables"][0]["rows"]
    assert rows[0] == ["Saudi Arabia (SA)", "120", "60.00%", "300", "5.00%", "2.30 s", "150 ms", "good"]
    assert rows[1] == ["United Arab Emirates (AE)", "50", "25.00%", "90", "20.00%", "4.20 s", "320 ms",
                       "poor"]
    assert rows[2] == ["Unknown", "30", "15.00%", "40", "0.00%", "—", "—", "no data"]
    chart = sec["charts"][0]["series"]
    assert chart["kind"] == "rank" and chart["points"][0] == {"label": "Saudi Arabia (SA)", "value": 120}
    # Sessions keyed by application + environment; vitals one sample per view.
    assert "uniqExactIf((application_id, env, session_id), sampled = 1)" in ch.call("GROUP BY country")[0]
    assert "final_view" in ch.call("GROUP BY dim_value")[0]


def test_geography_sort_top_n_and_summary_toggle(monkeypatch):
    sec = run(monkeypatch, "rum_geography", ch=geo_ch(), sort="error_rate", top_n=3, show_summary=False)
    assert sec["title"] == "Countries by Error-Session Rate"
    assert not sec["kpis"] and not sec["charts"]
    table = sec["tables"][0]
    assert table["title"] == "Countries by error-session rate — top 3 of 4 countries"
    assert not any(n.startswith("Country comes from") for n in sec["notes"])     # not repeated
    # XX has a 100 % rate over 3 sessions: ranked after countries with enough sessions.
    assert [r[0] for r in table["rows"]] == ["United Arab Emirates (AE)", "Saudi Arabia (SA)", "Unknown"]
    sec = run(monkeypatch, "rum_geography", ch=geo_ch(), sort="lcp")
    assert [r[0] for r in sec["tables"][0]["rows"]][:2] == ["United Arab Emirates (AE)", "Saudi Arabia (SA)"]


def test_geography_rollup(monkeypatch):
    rows = [("SA", 900, 2000, 30) + vit(2300, 800, 600, 50, rated=(700, 0, 0))]
    ch = FakeCH([("GROUP BY country", rows), ("uniqCombined64Merge(sessions)", [(1000, 31, 2100)])])
    sec = run(monkeypatch, "rum_geography", ch=ch, frm=FRM30)
    assert "apm_rum_metrics_5m" in ch.call("GROUP BY country")[0]
    assert sec["tables"][0]["rows"][0][:6] == ["Saudi Arabia (SA)", "900", "90.00%", "2,000", "3.33%", "2.30 s"]
    assert any("5-minute RUM aggregates" in n for n in sec["notes"])


def test_geography_empty(monkeypatch):
    sec = run(monkeypatch, "rum_geography")
    assert sec["notes"][0] == "No browser sessions were recorded in this period."


# ── rum_releases ────────────────────────────────────────────────────────────

REL_ROWS = [("2.4.0", 80, 200, 4, 9, FRM + timedelta(days=5), TO - timedelta(hours=1)),
            ("2.3.1", 150, 400, 3, 5, FRM, FRM + timedelta(days=5, hours=1)),
            ("", 20, 30, 0, 0, FRM, TO)]
REL_VITALS = [("2.4.0",) + vit(2100, 150, 130, 2, 160, 120, 110, 1, 0.02, 140, 138, 0),
              ("2.3.1",) + vit(2600, 300, 200, 20, 210, 280, 200, 10, 0.12, 290, 200, 30)]


def test_releases_newest_first_with_change_against_previous_release(monkeypatch):
    ch = FakeCH([("GROUP BY service_version", REL_ROWS), ("GROUP BY dim_value", REL_VITALS)])
    sec = run(monkeypatch, "rum_releases", ch=ch)
    assert kpi(sec, "Releases seen")["value"] == "2"
    latest = kpi(sec, "Latest release")
    assert latest["value"] == "2.4.0"
    assert latest["subtitle"] == "First seen " + BRAND.fmt_short(FRM + timedelta(days=5))
    err = kpi(sec, "Error-session rate (latest)")
    assert err["value"] == "5.00%" and err["accent"] == "warning"
    assert err["subtitle"] == "▲ 3.00 pts vs release 2.3.1"
    assert kpi(sec, "LCP p75 (latest)")["subtitle"] == "Good · ▼ 500 ms vs release 2.3.1"
    rows = sec["tables"][0]["rows"]
    assert rows[0] == ["2.4.0", BRAND.fmt_short(FRM + timedelta(days=5)), BRAND.fmt_short(TO - timedelta(hours=1)),
                       "80", "200", "5.00%", "2.10 s", "160 ms", "0.020"]
    assert rows[1][0] == "2.3.1" and rows[1][5] == "2.00%"
    assert any("20 session(s) carried no release version" in n for n in sec["notes"])
    assert "anyIf(raw.service_version" in ch.call("GROUP BY dim_value")[0]


def test_releases_sort_and_top_n(monkeypatch):
    ch = FakeCH([("GROUP BY service_version", REL_ROWS), ("GROUP BY dim_value", REL_VITALS)])
    sec = run(monkeypatch, "rum_releases", ch=ch, sort="sessions", top_n=1)
    assert sec["tables"][0]["rows"][0][0] == "2.3.1"
    assert sec["tables"][0]["title"].endswith("top 1 of 2 releases")


def test_releases_rollup(monkeypatch):
    rows = [("3.0.0", 500, 1500, 10, 25, FRM30, TO) + vit(2000, 400, 350, 10)]
    ch = FakeCH([("GROUP BY service_version", rows)])
    sec = run(monkeypatch, "rum_releases", ch=ch, frm=FRM30)
    sql, _p = ch.call("GROUP BY service_version")
    assert "apm_rum_metrics_5m" in sql and "uniqCombined64Merge(error_sessions)" in sql
    assert sec["tables"][0]["rows"][0][3:7] == ["500", "1,500", "2.00%", "2.00 s"]


# ── rum_slow_resources ──────────────────────────────────────────────────────

FAIL_AT = TO - timedelta(hours=5)


def resources_ch():
    return FakeCH([
        ("uniqExact((resource_type", [(500, 12, 40, 3, 850.0, 480)]),
        ("GROUP BY resource_type ORDER BY n DESC", [("script", 200, 2, 900.0, 190, 52000.0, 150),
                                                     ("fetch", 150, 10, 400.0, 150, float("nan"), 0)]),
        ("SELECT count() FROM (SELECT 1", [(25,)]),
        ("ORDER BY p75 DESC", [("script", "https://user:pw@cdn.example.com/app.js?v=1#x", 50, 2400.0, 48,
                                120000.0, 40, 1)]),
        ("HAVING nf > 0", [("fetch", "https://api.example.com/v1/cart?id=42", 30, 10, 0, FAIL_AT)]),
    ])


def test_slow_and_failing_resources(monkeypatch):
    ch = resources_ch()
    sec = run(monkeypatch, "rum_slow_resources", ch=ch)
    assert kpi(sec, "Resource requests")["value"] == "500"
    assert kpi(sec, "Resource requests")["subtitle"] == "40 distinct resources"
    assert kpi(sec, "Failed requests")["subtitle"] == "2.40% of requests"
    assert kpi(sec, "Failing resources")["value"] == "3"
    assert kpi(sec, "Resource p75 duration")["value"] == "850 ms"
    by_type, slow, failing = sec["tables"]
    assert by_type["rows"] == [["Script", "200", "2", "1.00%", "900 ms", "50.8 KB"],
                               ["Fetch", "150", "10", "6.67%", "400 ms", "—"]]
    assert slow["title"] == "Slowest resources (p75 duration) — top 1 of 25 resources"
    assert slow["rows"] == [["https://cdn.example.com/app.js", "Script", "50", "2.40 s", "117.2 KB", "2.00%"]]
    assert failing["rows"] == [["https://api.example.com/v1/cart", "Fetch", "30", "10", "33.33%",
                                "Network error", BRAND.fmt_short(FAIL_AT)]]
    sql, params = ch.call("ORDER BY p75 DESC")
    assert "cutQueryStringAndFragment(resource_url)" in sql and "sampled = 1" in sql
    assert params["min_req"] == 3 and params["lim"] == 10


def test_raw_only_sections_start_at_raw_retention(monkeypatch):
    for sid, needle in (("rum_slow_resources", "uniqExact((resource_type"),
                        ("rum_user_actions", "uniqExactIf(view_name, event_type = 'long_task')"),
                        ("rum_load_breakdown", "countIf(has_server_timing = 1), quantileTDigestIf"),
                        ("rum_issue_status", "ORDER BY n DESC")):
        ch = FakeCH()
        sec = run(monkeypatch, sid, ch=ch, frm=FRM30)
        start = ch.call(needle)[1]["f"]
        horizon = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=14)
        assert abs((start - horizon).total_seconds()) < 120, (sid, start)
        assert any("is kept for 14 days; this section covers" in n for n in sec["notes"]), sid
    # Within retention the period is read as is.
    ch = FakeCH()
    run(monkeypatch, "rum_slow_resources", ch=ch)
    assert ch.call("uniqExact((resource_type")[1]["f"] == FRM


def test_slow_resources_type_filter_and_empty(monkeypatch):
    ch = FakeCH()
    sec = run(monkeypatch, "rum_slow_resources", ch=ch, resource_type="script", min_requests=5)
    assert sec["notes"][0] == "No browser resource timings were recorded in this period."
    assert sec["description"].startswith("Scripts that real browsers loaded slowly")
    assert "resource_type = 'script'" in ch.call("uniqExact((resource_type")[0]


# ── rum_load_breakdown ──────────────────────────────────────────────────────

PAGE_PHASES = [(0.0, 5.0), (10.0, 8.0), (20.0, 15.0), (30.0, 25.0), (25.0, 20.0), (300.0, 250.0),
               (200.0, 180.0), (80.0, 60.0), (50.0, 40.0), (900.0, 800.0)]


def breakdown_ch(phases=PAGE_PHASES, slow=None):
    flat = tuple(v for pair in phases for v in pair)
    trend = [(local_day(0),) + (0.0, 8, 15, 25, 20, 250, 180, 60, 40, 800)[:len(phases)]]
    return FakeCH([
        ("countIf(has_server_timing = 1), quantileTDigestIf", [(200, 120, 3200.0, 200) + flat]),
        ("GROUP BY b", trend),
        ("SELECT count() FROM (SELECT 1", [(12,)]),
        ("ORDER BY p75 DESC", slow if slow is not None else
         [("/checkout", 40, 4100.0, 40, 350.0, 220.0, 20, 60.0)]),
    ])


def test_page_load_breakdown(monkeypatch):
    ch = breakdown_ch()
    sec = run(monkeypatch, "rum_load_breakdown", ch=ch)
    assert sec["title"] == "Page Load Breakdown"
    assert kpi(sec, "Measured page loads")["value"] == "200"
    assert kpi(sec, "Load time p75")["value"] == "3.20 s"
    largest = kpi(sec, "Largest phase")
    assert largest["value"] == "Browser processing"
    assert largest["subtitle"] == "avg 800 ms · 57.02% of measured time"
    assert kpi(sec, "Server-Timing coverage")["value"] == "60.00%"
    phases, slow = sec["tables"]
    assert [r[0] for r in phases["rows"]] == ["Redirect", "Queueing", "DNS lookup", "TCP connect",
                                              "TLS handshake", "Waiting for first byte", "Server processing",
                                              "Database", "Content download", "Browser processing"]
    assert phases["rows"][6] == ["Server processing", "200 ms", "180 ms", "12.83%", "120"]
    assert phases["styles"][3] == "pct-bar"
    assert slow["title"] == "Slowest pages by load time (p75) — top 1 of 12 pages"
    assert slow["rows"] == [["/checkout", "40", "4.10 s", "350 ms", "220 ms", "60 ms"]]
    chart = sec["charts"][0]
    assert chart["title"] == "Average time per phase by day (page loads)"
    assert chart["series"]["kind"] == "stacked" and chart["series"]["unit"] == "ms"
    names = [s["name"] for s in chart["series"]["series"]]
    assert "Redirect" not in names and names[0] == "Queueing"    # an all-zero phase is not drawn
    assert chart["series"]["series"][0]["points"][0]["t"] == iso(FRM)
    sql, params = ch.call("GROUP BY b")
    assert "INTERVAL 1 DAY" in sql and params["org_tz"] == "Asia/Riyadh"
    tot_sql = ch.call("countIf(has_server_timing = 1), quantileTDigestIf")[0]
    assert "event_type = 'view' AND is_final = 1 AND has_timing = 1" in tot_sql
    assert "greatest(connect_ms - tls_ms, 0)" in tot_sql            # TLS carved out of connect


def test_api_call_breakdown(monkeypatch):
    phases = PAGE_PHASES[:-1]
    ch = breakdown_ch(phases, slow=[("post", "https://api.example.com/v1/orders?token=abc", 30, 800.0, 30,
                                     500.0, 0.0, 0, 20.0)])
    sec = run(monkeypatch, "rum_load_breakdown", ch=ch, target="api", top_n=5)
    assert sec["title"] == "API Call Breakdown"
    assert "Browser processing" not in [r[0] for r in sec["tables"][0]["rows"]]
    assert sec["tables"][1]["headers"][0] == "Endpoint"
    assert sec["tables"][1]["rows"] == [["POST https://api.example.com/v1/orders", "30", "800 ms", "500 ms",
                                         "—", "20 ms"]]
    assert "resource_type IN ('fetch', 'xhr', 'xmlhttprequest')" in ch.call("ORDER BY p75 DESC")[0]
    assert ch.call("ORDER BY p75 DESC")[1]["lim"] == 5


def test_load_breakdown_empty(monkeypatch):
    sec = run(monkeypatch, "rum_load_breakdown")
    assert sec["notes"][0].startswith("No page loads with a timing breakdown")


# ── rum_funnel_conversion ───────────────────────────────────────────────────

CHECKOUT = [{"type": "view", "match": "/cart", "label": "Cart"},
            {"type": "view", "match": "/checkout/*", "label": "Checkout"},
            {"type": "action", "match": "Place order", "label": "Order placed"}]
SIGNUP = [{"type": "view", "match": "/signup"}, {"type": "view", "match": "/welcome"}]


def funnel_db():
    return FakeDB([("FROM rum_funnels", [("f1", "shop", "Checkout", json.dumps(CHECKOUT), 1800),
                                         ("f2", "shop", "Signup", SIGNUP, 3600)])])


def _funnel_levels(sql, p):
    return [(200, 120, 60, 500)] if p.get("step0") == "/cart" else [(0, 0, 40)]


def test_funnel_conversion(monkeypatch):
    db, ch = funnel_db(), FakeCH([("windowFunnel", _funnel_levels)])
    sec = run(monkeypatch, "rum_funnel_conversion", db=db, ch=ch)
    assert kpi(sec, "Funnels")["value"] == "2"
    assert kpi(sec, "Best conversion")["value"] == "30.00%"
    drop = kpi(sec, "Largest drop-off")
    assert drop["value"] == "50.00%" and drop["subtitle"] == "Checkout: before “Order placed”"
    summary, checkout, signup = sec["tables"]
    assert summary["rows"][0] == ["Checkout", "shop", "3", "200", "60", "30.00%", "Order placed (50.00%)"]
    assert summary["rows"][1] == ["Signup", "shop", "2", "0", "0", "—", "—"]
    assert checkout["title"] == "Checkout (within 30.0 min)"
    assert checkout["rows"] == [
        ["1. Cart", "Page: /cart", "200", "100.00%", "100.00%", "0"],
        ["2. Checkout", "Page: /checkout/*", "120", "60.00%", "60.00%", "80"],
        ["3. Order placed", "Action: Place order", "60", "30.00%", "50.00%", "60"]]
    assert signup["rows"][0][1] == "Page: /signup"
    sql, params = ch.all("windowFunnel")[0]
    assert params["step1"] == "^/checkout/[^/]+/?$" and "match(view_name, %(step1)s)" in sql
    assert "action_name = %(step2)s" in sql and params["step2"] == "Place order"
    assert params["fapp"] == "shop" and params["window_ms"] == 1_800_000
    assert "GROUP BY env, session_id" in sql and "sampled = 1" in sql
    assert sec["charts"][0]["series"]["points"] == [{"label": "Checkout", "value": 30.0}]


def test_funnel_application_filter_and_limit(monkeypatch):
    db, ch = funnel_db(), FakeCH([("windowFunnel", _funnel_levels)])
    sec = run(monkeypatch, "rum_funnel_conversion", db=db, ch=ch, filters={"application_id": "shop"},
              max_funnels=1)
    sql, params = db.call("FROM rum_funnels")
    assert "WHERE application_id = :app" in sql and params == {"app": "shop"}
    assert kpi(sec, "Funnels")["subtitle"] == "1 included"
    assert sec["tables"][0]["title"] == "Funnel summary — top 1 of 2 funnels"
    assert len(ch.all("windowFunnel")) == 1
    # One funnel: the chart shows its steps.
    assert sec["charts"][0]["series"]["points"][0] == {"label": "1. Cart", "value": 200}


def test_funnel_none_defined(monkeypatch):
    sec = run(monkeypatch, "rum_funnel_conversion")
    assert sec["notes"][0].startswith("No funnels are defined.")


# ── rum_user_actions ────────────────────────────────────────────────────────

def actions_ch():
    return FakeCH([
        ("uniqExactIf(view_name, event_type = 'long_task')", [(900, 12, 300, 80, 45000.0, 200, 60, 15, 40)]),
        ("GROUP BY action_name", [("Add to cart", "click", "/product/:id", 300, 120, 2, 180.0, 290),
                                  ("Buy", "rage_click", "/checkout", 12, 5, 0, float("nan"), 0)]),
        ("event_type = 'long_task' GROUP BY view_name", [("/checkout", 120, 40, 140.0, 900.0, 21000.0)]),
        ("GROUP BY b", [(local_day(0), 100, 5), (local_day(1), 200, 7)]),
    ])


def test_user_actions_and_long_tasks(monkeypatch):
    ch = actions_ch()
    sec = run(monkeypatch, "rum_user_actions", ch=ch)
    assert kpi(sec, "User actions")["subtitle"] == "in 200 sessions"
    assert kpi(sec, "Rage clicks")["value"] == "12"
    assert kpi(sec, "Long tasks")["value"] == "300"
    assert kpi(sec, "Main-thread blocking")["value"] == "45.00 s"
    actions, pages = sec["tables"]
    assert actions["title"] == "User actions — most frequent — top 2 of 60 actions"
    assert actions["rows"] == [["Add to cart", "Click", "/product/:id", "300", "120", "180 ms", "2"],
                               ["Buy", "Rage click", "/checkout", "12", "5", "—", "0"]]
    assert pages["rows"] == [["/checkout", "120", "40", "140 ms", "900 ms", "21.00 s"]]
    trend = next(c for c in sec["charts"] if c["title"] == "Long tasks and rage clicks by day")["series"]
    assert trend["kind"] == "bars" and trend["series"][0]["points"][0] == {"t": iso(FRM), "v": 100}
    assert "target" not in ch.call("GROUP BY action_name")[0]       # no element selectors


def test_user_actions_slowest_without_summary(monkeypatch):
    ch = actions_ch()
    sec = run(monkeypatch, "rum_user_actions", ch=ch, sort="duration", show_summary=False,
              show_long_tasks=False, top_n=5)
    assert not sec["kpis"] and not sec["charts"]
    assert len(sec["tables"]) == 1
    assert sec["tables"][0]["title"] == "User actions — slowest (p75 duration) — top 2 of 40 actions"
    sql, params = ch.call("GROUP BY action_name")
    assert "HAVING countIf(duration_ms > 0) > 0" in sql and "ORDER BY p75 DESC" in sql
    assert params["lim"] == 5
    assert not ch.all("event_type = 'long_task' GROUP BY view_name") and not ch.all("GROUP BY b")
    assert "long main-thread tasks" not in sec["description"]


def test_user_actions_empty(monkeypatch):
    sec = run(monkeypatch, "rum_user_actions")
    assert sec["notes"][0] == "No user actions or long tasks were recorded in this period."


# ── rum_issue_status ────────────────────────────────────────────────────────

SEEN = [("shop", "prod", "fp_new", 40, 12, FRM + timedelta(days=2), TO - timedelta(hours=2),
         "TypeError: x is undefined", "TypeError"),
        ("shop", "prod", "fp_open", 25, 10, FRM + timedelta(hours=1), TO - timedelta(hours=3),
         "ReferenceError: y is not defined", "ReferenceError"),
        ("shop", "prod", "fp_reg", 8, 3, FRM + timedelta(days=4), TO - timedelta(hours=1),
         "ChunkLoadError: loading chunk 7 failed", "ChunkLoadError"),
        ("shop", "prod", "fp_ign", 100, 50, FRM, TO - timedelta(hours=1), "ResizeObserver loop", "Error")]
HISTORY = [("shop", "prod", "fp_new", FRM + timedelta(days=2), "2.4.0", "TypeError: x is undefined", "TypeError"),
           ("shop", "prod", "fp_open", FRM - timedelta(days=3), "2.3.0", "ReferenceError", "ReferenceError"),
           ("shop", "prod", "fp_reg", FRM - timedelta(days=5), "2.2.0", "ChunkLoadError", "ChunkLoadError"),
           ("shop", "prod", "fp_ign", FRM - timedelta(days=6), "2.1.0", "ResizeObserver loop", "Error"),
           ("shop", "prod", "fp_res", FRM - timedelta(days=4), "2.3.1", "Old error", "Error")]
STORED = [("shop", "prod", "fp_reg", "resolved", "2.2.0", utc(FRM + timedelta(days=2))),
          ("shop", "prod", "fp_ign", "ignored", "", None),
          ("shop", "prod", "fp_res", "resolved", "", utc(FRM + timedelta(days=3)))]


def issues_fakes():
    ch = FakeCH([("ORDER BY n DESC", SEEN), ("fp IN %(fps)s", HISTORY)])
    db = FakeDB([("FROM rum_issues", STORED)])
    return db, ch


def test_issue_status_lifecycle(monkeypatch):
    db, ch = issues_fakes()
    sec = run(monkeypatch, "rum_issue_status", db=db, ch=ch)
    assert kpi(sec, "Issues seen")["value"] == "4"
    assert kpi(sec, "Issues seen")["subtitle"] == "173 errors in the period"
    assert [kpi(sec, s)["value"] for s in ("New", "Open", "Regressed", "Resolved", "Ignored")] == \
        ["1", "1", "1", "1", "1"]
    table = sec["tables"][0]
    assert table["headers"][:3] == ["Issue", "Application", "Type"]
    rows = table["rows"]
    assert [r[3] for r in rows] == ["regressed", "new", "open", "resolved", "ignored"]
    assert rows[0] == ["ChunkLoadError: loading chunk 7 failed", "shop · prod", "ChunkLoadError", "regressed",
                       "8", "3", BRAND.fmt_short(FRM - timedelta(days=5)),
                       BRAND.fmt_short(TO - timedelta(hours=1)), "2.2.0"]
    assert rows[1][8] == "2.4.0"                       # first release from history (argMin)
    # Resolved in the period and not seen since; no stored release, so the
    # earliest retained occurrence names it.
    assert rows[3] == ["Old error", "shop · prod", "Error", "resolved", "0", "0",
                       BRAND.fmt_short(FRM - timedelta(days=4)), "—", "2.3.1"]
    assert sec["charts"][0]["series"]["kind"] == "donut"
    hist_sql, hist_params = ch.call("fp IN %(fps)s")
    assert hist_params["fps"] == ("fp_ign", "fp_new", "fp_open", "fp_reg", "fp_res")
    assert "error_fingerprint" in hist_sql


def test_issue_status_filter_and_application(monkeypatch):
    db, ch = issues_fakes()
    sec = run(monkeypatch, "rum_issue_status", db=db, ch=ch, status="new_regressed",
              filters={"application_id": "shop"})
    # One application: no Application column.
    assert sec["tables"][0]["headers"][:2] == ["Issue", "Type"]
    assert [r[2] for r in sec["tables"][0]["rows"]] == ["regressed", "new"]
    assert sec["tables"][0]["title"].startswith("New and regressed")
    sql, params = db.call("FROM rum_issues")
    assert "WHERE application_id = :app" in sql and params == {"app": "shop"}
    assert ch.call("ORDER BY n DESC")[1]["app"] == "shop"


def test_issue_status_cannot_call_new_beyond_retention(monkeypatch):
    db, ch = issues_fakes()
    sec = run(monkeypatch, "rum_issue_status", db=db, ch=ch, frm=FRM30)
    assert kpi(sec, "New")["value"] == "0" and kpi(sec, "Open")["value"] == "2"
    assert any("retention horizon" in n for n in sec["notes"])
    assert any("Browser error detail is kept for 14 days" in n for n in sec["notes"])


def test_issue_status_empty(monkeypatch):
    sec = run(monkeypatch, "rum_issue_status")
    assert sec["notes"][0] == "No browser JavaScript errors were reported in this period."


@pytest.mark.parametrize("sid", SECTIONS)
def test_sections_survive_no_data_with_application_filter(monkeypatch, sid):
    ch = FakeCH()
    sec = run(monkeypatch, sid, ch=ch, filters={"application_id": "shop"})
    assert sec["notes"], sid
    assert sec["description"], sid
