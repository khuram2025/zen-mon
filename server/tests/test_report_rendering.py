"""Branded report output: chart images, the HTML document, the PDF document
and the legacy PDF pipeline. Everything runs on fakes (no database, no
ClickHouse, no network)."""

from __future__ import annotations

import asyncio
import base64
import io
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import report_charts as rc
from app.services import report_sections as rs
from app.services import report_service
from app.services.branding import Branding, Logo

TO = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
FROM = TO - timedelta(days=7)

SVG_LOGO = (b'<?xml version="1.0"?>\n<!-- brand -->\n<svg xmlns="http://www.w3.org/2000/svg" '
            b'viewBox="0 0 300 80" width="300" height="80"><rect width="300" height="80" rx="12" '
            b'fill="#0E7490"/><circle cx="40" cy="40" r="22" fill="#fff"/></svg>')


def _png_logo() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (240, 80), (14, 116, 144)).save(buf, "PNG")
    return buf.getvalue()


def _branding(**kw) -> Branding:
    base = dict(company_name="Acme Networks", timezone="Asia/Riyadh", brand_color="#0E7490",
                confidentiality="Confidential", report_footer="Acme NOC",
                report_disclaimer="Figures are computed from monitoring samples.",
                company_website="acme.example")
    base.update(kw)
    return Branding(**base)


def _meta(title: str = 'Weekly "Ops" <Review>', **kw) -> dict:
    return asyncio.run(rs.build_report_meta(
        None, title, FROM, TO, description="Availability for the week.", category="Operations",
        scope_label="All monitored infrastructure", branding=_branding(**kw)))


def _ts(i: int, hours: int = 2) -> str:
    return (FROM + timedelta(hours=hours * i)).isoformat().replace("+00:00", "Z")


AREA = {"kind": "area", "unit": "%", "color": "success", "y_domain": [95, 100],
        "points": [{"t": _ts(i), "v": None if i in (5, 6) else 99.5 - (i % 5) * 0.2} for i in range(80)]}
SERIES = {
    "area": AREA,
    "line": {"kind": "line", "unit": "Mbps",
             "series": [{"name": f"Link {k}", "points": [{"t": _ts(i, 1), "v": (i * (k + 1)) % 17}
                                                         for i in range(48)]} for k in range(3)]},
    "bars": {"kind": "bars", "unit": "alerts", "color": "warning",
             "points": [{"t": _ts(d, 24), "v": d % 4} for d in range(7)]},
    "stacked": {"kind": "stacked", "unit": "captures",
                "series": [{"name": "Succeeded", "points": [{"t": _ts(d, 24), "v": 20 + d} for d in range(7)]},
                           {"name": "Failed", "points": [{"t": _ts(d, 24), "v": d % 3} for d in range(7)]}]},
    "rank": {"kind": "rank", "unit": "GB",
             "points": [{"label": f"host-{i}", "value": 100 / (i + 1)} for i in range(25)]},
    "donut": {"kind": "donut", "unit": "nodes",
              "points": [{"label": "Up", "value": 40}, {"label": "Down", "value": 3},
                         {"label": "Unknown", "value": 1}]},
    "donut_many": {"kind": "donut", "unit": "devices",
                   "points": [{"label": f"type {i}", "value": 10 - i} for i in range(9)]},
    "bars_by_label": {"kind": "bars", "points": [{"label": "a", "value": 3}, {"label": "b", "value": 5}]},
}


# ─── chart_png ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(SERIES))
@pytest.mark.parametrize("width", [178, 85])
def test_chart_png_renders_every_kind(name, width):
    png = rc.chart_png(SERIES[name], title="Chart" if width == 178 else "",
                       branding=_branding(), width_mm=width, height_mm=56)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    w_mm, h_mm = rc.png_size_mm(png)
    assert abs(w_mm - width) < 0.6  # rendered at its exact physical width (200 dpi)
    assert h_mm > 15


@pytest.mark.parametrize("kind", rc.KINDS)
@pytest.mark.parametrize("series", [
    {"points": []},
    {"points": [{"t": _ts(1), "v": None}, {"t": _ts(2), "v": None}]},
    {"points": [{"label": "x", "value": 0}]},
    {"series": []},
    {"points": [{"t": "not a time", "v": "nan"}, "junk", None]},
])
def test_chart_png_empty_or_unreadable_series_is_a_placeholder(kind, series):
    png = rc.chart_png({"kind": kind, **series}, branding=None)
    assert png.startswith(b"\x89PNG")


def test_chart_png_tolerates_garbage_input():
    for bad in (None, {}, {"kind": "bogus", "points": [{"t": 1, "v": 2}]}, {"kind": "rank", "points": "x"}):
        assert rc.chart_png(bad).startswith(b"\x89PNG")


def test_chart_png_is_cached_per_content_and_size():
    a = rc.chart_png(AREA, branding=_branding(), width_mm=178, height_mm=56)
    b = rc.chart_png(AREA, branding=_branding(), width_mm=178, height_mm=56)
    c = rc.chart_png(AREA, branding=_branding(), width_mm=85, height_mm=50)
    assert a == b and a != c


def test_monitoring_gaps_and_nulls_are_drawn_as_gaps():
    ts = [FROM + timedelta(minutes=5 * i) for i in (0, 1, 2, 3, 10, 11)]
    vals = rc.np.array([1.0, 2.0, None, 4.0, 5.0, 6.0], dtype=float)
    out_t, out_v = rc._with_gaps(ts, vals)
    assert len(out_t) == len(ts) + 1  # one gap inserted for the 35-minute hole
    assert sum(1 for v in out_v if v != v) == 2  # the null and the gap are NaN, never 0


def test_time_context_names_the_organisation_zone():
    ctx = rc._Ctx(_branding())
    assert rc._time_context(ctx, FROM, TO).endswith("times in GMT+3")
    assert rc._time_context(rc._Ctx(None), FROM, TO).endswith("times in UTC")


def test_status_categories_use_reserved_colours_and_brand_is_not_a_data_colour():
    assert rc.status_color("Down") == report_service.HEX_DANGER
    assert rc.status_color("type 1") is None
    colors = rc._series_colors([{"name": "Up", "color": None}, {"name": "Down", "color": None}], "#000000")
    assert colors == [report_service.HEX_SUCCESS, report_service.HEX_DANGER]
    assert rc.MARK_HEX["primary"] == report_service.CATEGORICAL_HEX[0]


# ─── colour helpers ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("hex_", ["#FACC15", "#FFFFFF", "#22D3EE", "#4F6BF6", "#000000", "#DC2626"])
def test_brand_accent_reaches_3_to_1_on_white(hex_):
    from app.services.branding import hex_to_rgb
    t = report_service.brand_tokens(hex_to_rgb(hex_))
    assert report_service.contrast_ratio(t["accent"], (255, 255, 255)) >= 3.0
    assert report_service.contrast_ratio(t["on_accent"], t["accent"]) >= 3.0
    assert report_service.contrast_ratio((255, 255, 255), t["ground"]) >= 7.0


# ─── HTML ───────────────────────────────────────────────────────────────────

def _sections(rows: int = 130) -> list[dict]:
    table_rows = [[f"node-{i:03d}", f"10.0.0.{i % 250}", "up" if i % 5 else "down",
                   f"{99.9 - (i % 9) * 0.3:.2f}%", f"{i} min"] for i in range(rows)]
    return [
        {"id": "availability_kpis", "title": "Availability Summary", "category": "Availability",
         "description": "Computed over all monitored nodes.",
         "kpis": [{"label": "Availability", "value": "99.71%", "accent": "success", "subtitle": "Target 99.9%"},
                  {"label": "Outages", "value": "42", "accent": "primary"}],
         "charts": [], "tables": [], "notes": ["Planned maintenance excluded."]},
        {"id": "availability_trend", "title": "Availability Trend", "kpis": [], "tables": [], "notes": [],
         "charts": [{"title": "Fleet availability", "series": AREA}]},
        {"id": "alert_trend", "title": "Alert Volume", "half": True, "kpis": [], "tables": [], "notes": [],
         "charts": [{"title": "", "series": SERIES["bars"]}]},
        {"id": "device_status_now", "title": "Current Device Status", "half": True, "kpis": [],
         "tables": [], "notes": [], "charts": [{"title": "Nodes by state", "series": SERIES["donut"]}]},
        {"id": "device_availability", "title": "Node Availability", "kpis": [], "charts": [], "notes": [],
         "tables": [{"headers": ["Node", "IP address", "Status", "Availability", "Downtime"],
                     "styles": ["text", "mono", "status", "pct-bar", "num"], "rows": table_rows}]},
        {"id": "ncm_protection", "title": "Configuration Protection", "restricted": True, "kpis": [],
         "charts": [], "tables": [],
         "notes": ["Not included: viewing this section requires the ncm.view permission."]},
        {"id": "server_performance", "title": "Server Performance", "error": True, "kpis": [], "charts": [],
         "tables": [], "notes": ["This section could not be generated (OperationalError)."]},
    ]


def test_render_html_is_branded_escaped_and_complete():
    meta = _meta()
    html = rs.render_html(meta, _sections())
    assert html.startswith("<!DOCTYPE html>")
    # brand colour drives the CSS variables; timezone label and zone are printed
    assert "--brand:#0E7490" in html
    assert "GMT+3" in html and "Asia/Riyadh" in html
    # the title is escaped, quotes included
    assert "Weekly &quot;Ops&quot; &lt;Review&gt;" in html
    assert "<Review>" not in html and 'Weekly "Ops"' not in html
    # classification badge, KPI subtitles, numbered contents with anchors
    assert 'class="badge">Confidential<' in html
    assert "Target 99.9%" in html
    assert 'href="#s-01"' in html and 'id="s-07"' in html
    # every table row (no silent 60-row cut)
    assert "node-129" in html and "Showing the first" not in html
    assert html.count('class="pill pill--danger">down<') == 26
    assert 'class="pct"' in html and "width:99.9%" in html
    # restricted sections are muted cards; errors differ from info notes
    assert "section--restricted" in html and "Not included" in html
    assert "Viewing this section requires the ncm.view permission." in html
    assert "callout--error" in html and "callout--info" in html
    # series-only charts become inline PNG images; half sections sit side by side
    assert html.count('<img src="data:image/png;base64,') == 3
    assert '<div class="pair">' in html
    # disclaimer and footer
    assert "Figures are computed from monitoring samples." in html
    assert "Acme NOC" in html and "Generated by ZenPlus" in html
    # print: paper size, margins and page counter
    assert "size:A4" in html and "counter(pages)" in html


def test_render_html_has_no_external_resources():
    html = rs.render_html(_meta(company_website="https://acme.example"), _sections())
    for attr in re.findall(r'(?:src|href)="([^"]*)"', html):
        assert attr.startswith(("data:", "#")), attr
    lowered = html.lower()
    for forbidden in ("<script", "<link", "@import", "url(", "<iframe", "<object"):
        assert forbidden not in lowered, forbidden


def test_render_html_caps_very_long_tables_with_an_explicit_note():
    html = rs.render_html(_meta(), _sections(rows=620))
    assert "node-499" in html and "node-500" not in html
    assert "Showing the first 500 of 620 rows" in html


def test_render_html_logo_mime_and_fallback():
    with_svg = rs.render_html(_meta(logo=Logo(SVG_LOGO, "image/svg+xml")), _sections()[:1])
    assert 'src="data:image/svg+xml;base64,' in with_svg
    with_png = rs.render_html(_meta(logo=Logo(_png_logo(), "image/png")), _sections()[:1])
    assert 'src="data:image/png;base64,' in with_png and 'class="tile"' in with_png
    plain = rs.render_html(_meta(), _sections()[:1])
    assert '<span class="name">Acme Networks</span>' in plain
    # an SVG with only a viewBox has no intrinsic size: the logo box sets a height
    assert ".brand img{display:block;height:44px;" in with_svg
    assert ".brand .tile img{height:32px;" in with_svg


def test_render_html_light_brand_accent_is_darkened_and_letter_paper():
    html = rs.render_html(_meta(brand_color="#FACC15", paper_size="Letter", confidentiality=""), _sections()[:2])
    accent = re.search(r"--accent:(#[0-9A-F]{6})", html).group(1)
    assert accent != "#FACC15"
    from app.services.branding import hex_to_rgb
    assert report_service.contrast_ratio(hex_to_rgb(accent), (255, 255, 255)) >= 3.0
    assert "size:letter" in html and 'class="badge"' not in html


def test_render_html_works_without_branding_object():
    meta = {"title": "Plain", "period_label": "p", "generated_label": "g"}
    html = rs.render_html(meta, _sections()[:2])
    assert "<title>Plain · ZenPlus</title>" in html and "All times UTC" in html


def test_esc_escapes_quotes():
    assert rs._esc('a"b\'c<d>&') == "a&quot;b&#x27;c&lt;d&gt;&amp;"
    assert rs._esc(None) == ""
    assert '"' not in rs._css_str('x"</style><b>')[1:-1]
    assert "<" not in rs._css_str("</style>")


# ─── PDF ────────────────────────────────────────────────────────────────────

def _mediabox(pdf: bytes) -> bytes:
    return re.search(rb"/MediaBox \[([^\]]*)\]", pdf).group(1)


@pytest.mark.parametrize("logo", [None, "png", "svg", "dark"])
def test_render_pdf_with_png_svg_dark_and_no_logo(logo):
    kw = {}
    if logo == "png":
        kw["logo"] = Logo(_png_logo(), "image/png")
    elif logo == "svg":
        kw["logo"] = Logo(SVG_LOGO, "image/svg+xml")
    elif logo == "dark":
        kw["logo"] = Logo(_png_logo(), "image/png")
        kw["logo_dark"] = Logo(_png_logo(), "image/png")
    pdf = rs.render_pdf(_meta(**kw), _sections())
    assert pdf.startswith(b"%PDF")
    assert b"/Author (Acme Networks)" in pdf


def test_render_pdf_letter_and_a4_page_sizes_differ():
    a4 = rs.render_pdf(_meta(paper_size="A4"), _sections()[:2])
    letter = rs.render_pdf(_meta(paper_size="Letter"), _sections()[:2])
    assert _mediabox(a4) == b"0 0 595.28 841.89"
    assert _mediabox(letter) == b"0 0 612.00 792.00"
    doc = report_service.ZenPlusReport.from_meta(_meta(paper_size="Letter"))
    assert (round(doc.page_w, 1), round(doc.page_h, 1)) == (215.9, 279.4)
    assert round(doc.body_w, 1) == round(215.9 - 32, 1)


def test_render_pdf_includes_series_only_charts():
    no_charts = rs.render_pdf(_meta(), [s for s in _sections() if not s["charts"]])
    with_charts = rs.render_pdf(_meta(), _sections())
    assert no_charts.count(b"/Subtype /Image") == 0
    assert with_charts.count(b"/Subtype /Image") >= 3


def test_document_takes_branding_from_meta():
    meta = _meta(brand_color="#FACC15")
    doc = report_service.ZenPlusReport.from_meta(meta)
    assert doc.brand == (250, 204, 21)
    assert doc.accent != doc.brand
    assert report_service.contrast_ratio(doc.accent, (255, 255, 255)) >= 3.0
    assert doc.generated_label == meta["generated_label"] and "GMT+3" in doc.generated_label
    assert doc.tz_note == "All times GMT+3 (Asia/Riyadh)"
    assert doc.classification == "Confidential" and doc.doc_label == "Operations"
    assert "UTC" not in doc.tz_note + doc.generated_label + doc.period_label


def test_render_pdf_survives_the_core_font_fallback(monkeypatch):
    """Without the embedded TTFs (core Helvetica) text is folded to Latin-1."""
    monkeypatch.setattr(report_service, "UNICODE_FONTS", False)
    meta = _meta(title="تقرير — “Ops” …", company_name="شركة")
    assert rs.render_pdf(meta, _sections()).startswith(b"%PDF")


def test_render_pdf_numbering_and_contents_for_four_or_more_sections():
    calls = []
    orig = report_service.ZenPlusReport.toc

    def spy(self, expected_entries):
        calls.append(expected_entries)
        return orig(self, expected_entries)

    report_service.ZenPlusReport.toc = spy
    try:
        rs.render_pdf(_meta(), _sections()[:3])
        rs.render_pdf(_meta(), _sections()[:4])
    finally:
        report_service.ZenPlusReport.toc = orig
    assert calls == [4]


# ─── Legacy pipeline (report_service.generate_report) ───────────────────────

class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def fetchone(self):
        return self.first()


class _LegacyDB:
    def __init__(self, company: dict, logo: bytes | None):
        self.company, self.logo, self.sql = company, logo, []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.sql.append(sql)
        if "FROM system_settings" in sql:
            rows = [("company", self.company)]
            if self.logo:
                rows.append(("company_logo", {"data": base64.b64encode(self.logo).decode()}))
            return _Result(rows)
        if "FROM alerts a" in sql:
            t = TO - timedelta(hours=30)
            return _Result([SimpleNamespace(_mapping={
                "id": i, "device_id": "d1", "service_check_id": None, "status": "resolved",
                "severity": "critical" if i % 2 else "warning", "message": "Link down",
                "triggered_at": t + timedelta(hours=i), "acknowledged_at": None,
                "resolved_at": t + timedelta(hours=i, minutes=12), "hostname": "core-1",
                "metadata": {}}) for i in range(4)])
        return _Result([])


class _EmptyCH:
    def query(self, sql, parameters=None, **kw):
        return SimpleNamespace(column_names=(), result_rows=[])


def test_legacy_generate_report_uses_organisation_branding(monkeypatch):
    from app.services import report_data_service

    monkeypatch.setattr(report_service, "get_ch_client", lambda: _EmptyCH())
    monkeypatch.setattr(report_data_service, "device_outage_episodes", lambda *a, **k: [])
    seen: dict = {}
    orig_cover = report_service.ZenPlusReport.cover

    def spy(self, *a, **k):
        seen.update(company=self.company_name, paper=self.paper_size, brand=self.brand,
                    period=self.period_label, generated=self.generated_label, tz=self.tz_note,
                    classification=self.classification, logo=bool(self._logo))
        return orig_cover(self, *a, **k)

    monkeypatch.setattr(report_service.ZenPlusReport, "cover", spy)
    db = _LegacyDB({"company_name": "Acme Networks", "timezone": "Asia/Riyadh", "brand_color": "#DC2626",
                    "paper_size": "Letter", "confidentiality": "Internal",
                    "report_disclaimer": "Sample disclaimer."}, _png_logo())
    for report_type in ("alert_analysis", "full_report"):
        pdf = bytes(asyncio.run(report_service.generate_report(db, report_type, "last_7d")))
        assert pdf.startswith(b"%PDF")
        assert _mediabox(pdf) == b"0 0 612.00 792.00"
    assert any("FROM system_settings" in s for s in db.sql)
    assert seen["company"] == "Acme Networks" and seen["paper"] == "Letter"
    assert seen["brand"] == (220, 38, 38) and seen["logo"]
    assert seen["period"].startswith("Last 7 Days · ") and seen["period"].endswith("GMT+3")
    assert "GMT+3" in seen["generated"] and "UTC" not in seen["generated"]
    assert seen["tz"] == "All times GMT+3 (Asia/Riyadh)" and seen["classification"] == "Internal"


def test_legacy_status_log_rows_use_the_organisation_timezone():
    rows = report_service._status_log_rows(
        [{"timestamp": datetime(2026, 10, 7, 21, 30), "old_status": "up", "new_status": "down",
          "reason": "ping", "duration_sec": 0}], branding=_branding(date_format="DD/MM/YYYY"))
    assert rows[0][0] == "08/10/2026 00:30"


def test_legacy_fetch_company_info_reads_sanitised_branding():
    db = _LegacyDB({"company_name": "   ", "timezone": "Not/AZone"}, _png_logo())
    info = asyncio.run(report_service._fetch_company_info(db))
    assert info["company_name"] == "ZenPlus"  # an empty stored name falls back
    assert info["timezone"] == "UTC" and info["logo_mime"] == "image/png"
    assert info["logo_bytes"].startswith(b"\x89PNG")


def test_legacy_chart_helpers_are_pyplot_free_and_tz_aware():
    from matplotlib import _pylab_helpers

    ts = [FROM + timedelta(hours=i) for i in range(30)]
    pngs = [
        report_service._make_line_chart(ts, [float(i % 7) for i in range(30)], unit="ms",
                                        tz=_branding().tz),
        report_service._make_time_bar_chart(ts[:7], [1, 2, 3, 0, 2, 1, 4]),
        report_service._make_bar_chart(["a", "b"], [3, 1]),
        report_service._make_donut(["Up", "Down"], [5, 1], center_text="83%"),
        report_service._empty_chart(),
    ]
    assert all(p.startswith(b"\x89PNG") for p in pngs)
    assert _pylab_helpers.Gcf.get_num_fig_managers() == 0  # nothing left in a global registry


# ─── Layout details ─────────────────────────────────────────────────────────

def _axes():
    from matplotlib.figure import Figure
    return Figure(figsize=(4, 2)).add_subplot()


def test_daily_bars_get_a_tick_under_every_bar_when_there_is_room():
    import matplotlib.dates as mdates

    ctx = rc._Ctx(_branding())
    keys = [datetime(2026, 9, 30, 21, tzinfo=timezone.utc) + timedelta(days=d) for d in range(7)]
    ax = _axes()
    rc._bar_ticks(ax, ctx, keys, mdates.date2num(keys), 1.0, slot_mm=10.0)
    assert len(ax.get_xticks()) == 7
    ax = _axes()
    rc._bar_ticks(ax, ctx, keys, mdates.date2num(keys), 1.0, slot_mm=5.0)
    assert len(ax.get_xticks()) == 4  # every second day
    ax = _axes()
    before = list(ax.get_xticks())
    rc._bar_ticks(ax, ctx, keys, mdates.date2num(keys), 1.0, slot_mm=2.0)
    assert list(ax.get_xticks()) == before  # weekly would leave one tick: keep the locator's


def test_hourly_bar_ticks_align_to_the_local_clock():
    import matplotlib.dates as mdates

    ctx = rc._Ctx(_branding())  # Asia/Riyadh, GMT+3
    keys = [datetime(2026, 10, 6, 10, tzinfo=timezone.utc) + timedelta(hours=h) for h in range(24)]
    ax = _axes()
    rc._bar_ticks(ax, ctx, keys, mdates.date2num(keys), 1 / 24, slot_mm=3.0)  # every 3rd bar
    hours = [mdates.num2date(t, tz=ctx.tz).hour for t in ax.get_xticks()]
    assert hours and all(h % 3 == 0 for h in hours)


def test_pdf_heading_moves_with_its_first_block():
    doc = report_service.ZenPlusReport.from_meta(_meta())
    doc.add_page()
    doc.set_y(doc.page_break_trigger - 45)
    doc.section_title("Kept together", description="A short description.", keep_with=40)
    assert doc.page_no() == 2  # heading + 40 mm do not fit: both start the next page
    doc.set_y(doc.page_break_trigger - 45)
    doc.section_title("Fits", keep_with=10)
    assert doc.page_no() == 2


def test_first_block_height_covers_charts_tables_and_kpis():
    doc = report_service.ZenPlusReport.from_meta(_meta())
    secs = {s["id"]: s for s in _sections()}
    assert 45 < rs._first_block_mm(doc, secs["availability_trend"]) < 80  # chart + title
    assert rs._first_block_mm(doc, secs["device_availability"]) > 30  # header + first rows
    assert rs._first_block_mm(doc, secs["availability_kpis"]) == pytest.approx(27.0)
    assert rs._first_block_mm(doc, secs["ncm_protection"]) > 15


def test_pdf_contents_marks_restricted_sections(monkeypatch):
    seen = []
    orig = report_service._render_toc

    def spy(pdf, outline):
        seen.append((set(pdf._toc_muted), [o.name for o in outline if o.level == 0]))
        return orig(pdf, outline)

    monkeypatch.setattr(report_service, "_render_toc", spy)
    rs.render_pdf(_meta(), _sections())
    muted, names = seen[0]
    assert names[5] == "Configuration Protection" and muted == {6}
    assert len(names) == len(_sections())


@pytest.mark.parametrize("fmt", ["WEBP", "JPEG"])
def test_render_pdf_accepts_webp_and_jpeg_logos(fmt):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (300, 100), (200, 30, 30)).save(buf, fmt)
    logo = Logo(buf.getvalue(), f"image/{fmt.lower()}")
    doc = report_service.ZenPlusReport.from_meta(_meta(logo=logo))
    assert doc._image_size(logo.data) == (300.0, 100.0)
    assert rs.render_pdf(_meta(logo=logo), _sections()[:2]).startswith(b"%PDF")


def test_unreadable_logo_falls_back_to_the_company_name():
    broken = Logo(b'<svg xmlns="http://www.w3.org/2000/svg"><g><broken', "image/svg+xml")
    doc = report_service.ZenPlusReport.from_meta(_meta(logo=broken))
    assert doc._image_size(broken.data) is None
    doc.add_page()
    assert doc._cover_brand(16, 14) is False  # cover() then prints the name
    assert rs.render_pdf(_meta(logo=broken), _sections()[:2]).startswith(b"%PDF")


def test_html_print_rules_keep_headings_and_pairs_together():
    html = rs.render_html(_meta(), _sections())
    assert ".sec-head{break-inside:avoid;break-after:avoid}" in html
    assert ".disclaimer,.pair{break-inside:avoid}" in html
    assert "thead{display:table-header-group}" in html


def test_html_header_less_table_still_shows_its_rows():
    sec = {"id": "x", "title": "Facts", "kpis": [], "charts": [], "notes": [],
           "tables": [{"headers": [], "rows": [["Vendor", "Cisco"], ["Model", "C9300"]]}]}
    html = rs.render_html(_meta(), [sec])
    assert "C9300" in html and "<thead>" not in html
    assert rs.render_pdf(_meta(), [sec]).startswith(b"%PDF")


def test_render_pdf_wraps_wide_tables_and_kpi_labels_instead_of_truncating(monkeypatch):
    # Assessment L1: a 9-column table cut "Rahoma Branch Internet Uplink" to
    # "Raho …" and headers to "CRITICALI…"; KPI cards cut "BELOW CONFIGURED…".
    drawn: list[str] = []
    orig = rs.SectionReportPDF.cell

    def spy(self, w=None, h=None, text="", *a, **kw):
        drawn.append(str(text))
        return orig(self, w, h, text, *a, **kw)
    monkeypatch.setattr(rs.SectionReportPDF, "cell", spy)
    service = "Rahoma Branch Internet Uplink (Primary MPLS circuit)"
    headers = ["Service", "Owner", "Criticality", "Target (source)", "Availability", "Coverage",
               "Downtime", "Error budget used", "SLA status"]
    rows = [[service, "SMASCO Network Operations Team", "critical", "99.9% (policy default)",
             "99.95%", "100.00%", "12 min", "41%", "met"]]
    sec = {"id": "service_owner_sla", "title": "Service SLA compliance",
           "kpis": [{"label": "Below configured target (99.9%)", "value": "3", "accent": "danger",
                     "subtitle": "Current status, at generation — every node in scope"}],
           "charts": [], "notes": [],
           "tables": [{"headers": headers,
                       "styles": ["text", "text", "severity", "text", "pct-bar", "num", "num", "num",
                                  "status"], "rows": rows}]}
    pdf = rs.render_pdf(_meta(), [sec])
    assert pdf.startswith(b"%PDF")
    text = " ".join(drawn)
    assert service in text                                   # wrapped, rejoined by the spaces
    assert "SMASCO Network Operations Team" in text
    assert "CRITICALITY" in text and "TARGET (SOURCE)" in text
    assert "BELOW CONFIGURED TARGET (99.9%)" in text
    assert "Current status, at generation" in text
    assert not any(t.endswith("…") for t in drawn if t), [t for t in drawn if t.endswith("…")]


def test_wrap_splits_words_too_long_for_a_line():
    doc = rs.SectionReportPDF(title="t")
    doc.add_page()
    doc._sans("", 7.6)
    lines = doc._wrap("x" * 200, 20, max_lines=50)
    assert "".join(lines) == "x" * 200 and all(doc.get_string_width(ln) <= 20 for ln in lines)
    capped = doc._wrap("word " * 100, 20, max_lines=2)
    assert len(capped) == 2 and capped[-1].endswith("…")


def test_render_pdf_long_cells_stay_fast():
    # Review: a 20,000-character token took minutes to wrap, and 500 rows of
    # 1,000-word cells about 50 s.
    import time
    token = "x" * 20_000
    words = " ".join(f"word{i}" for i in range(1_000))
    sec = {"id": "t", "title": "Long cells", "kpis": [{"label": token, "value": "1", "subtitle": words}],
           "charts": [], "notes": [],
           "tables": [{"headers": ["Name", "Detail", "Status", "Share"],
                       "styles": ["text", "text", "status", "pct-bar"],
                       "rows": [[token, words, words, f"{i % 100}% {words}"] for i in range(500)]}]}
    t0 = time.monotonic()
    pdf = rs.render_pdf(_meta(), [sec])
    assert pdf.startswith(b"%PDF")
    assert time.monotonic() - t0 < 3.0  # the whole document, 500 rows (about 0.6 s)
    doc = rs.SectionReportPDF(title="t")
    doc.add_page()
    doc._sans("", 7.6)
    t0 = time.monotonic()
    lines = doc._wrap(token, 30)
    many = [doc._wrap(words, 30) for _ in range(500)]
    assert time.monotonic() - t0 < 2.0
    assert len(lines) == 4 and lines[-1].endswith("…") and all(len(m) == 4 for m in many)
