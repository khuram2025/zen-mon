"""Scheduler tick: a due schedule is claimed (next_run_at advanced and
committed) before generation, and timer-fired custom schedules keep their
custom_report_id."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services import report_scheduler as rs


class TickDB:
    def __init__(self, rows):
        self.rows = rows
        self.log: list[str] = []

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.log.append(sql[:40])
        rows = self.rows

        class R:
            def fetchall(self):
                return rows if sql.startswith("SELECT * FROM report_schedules") else []

            def first(self):
                if not sql.startswith("SELECT * FROM report_schedules"):
                    return None
                done = set((params or {}).get("done") or [])
                return next((r for r in rows if str(r.id) not in done), None)

        return R()

    async def commit(self):
        self.log.append("COMMIT")

    async def rollback(self):
        self.log.append("ROLLBACK")


def _row(**kw):
    base = dict(id="s1", name="Monthly SLA", enabled=True, report_type="custom", period="last_30d",
                format="pdf", filters={}, frequency="daily", hour=8, minute=0, day_of_week=None,
                day_of_month=None, notify_channels=[], custom_report_id="c-123")
    base.update(kw)
    return SimpleNamespace(**base)


def test_tick_claims_before_generating_and_keeps_custom_id(monkeypatch):
    seen = {}

    async def fake_tz(db):
        return "Asia/Riyadh"

    async def fake_generate(db, sched, **kw):
        seen["sched"] = sched
        seen["log_at_generate"] = list(db.log)
        return {"status": "success", "errors": []}

    monkeypatch.setattr(rs, "get_configured_timezone", fake_tz)
    monkeypatch.setattr(rs, "generate_and_deliver", fake_generate)
    db = TickDB([_row()])
    out = asyncio.run(rs.run_scheduler_tick(db))

    assert out["fired"] == ["s1"]
    assert seen["sched"]["custom_report_id"] == "c-123"
    # The claim (UPDATE next_run_at) was committed before generation started.
    before = seen["log_at_generate"]
    assert before[-1] == "COMMIT" and any(s.startswith("UPDATE report_schedules") for s in before)


def test_tick_failure_records_class_name_not_message(monkeypatch):
    async def fake_tz(db):
        return "UTC"

    async def boom(db, sched, **kw):
        raise RuntimeError("password=secret in DSN")

    captured = []
    db = TickDB([_row(report_type="availability", custom_report_id=None)])
    orig = db.execute

    async def spy(statement, params=None):
        if params and "st" in params:
            captured.append(params)
        return await orig(statement, params)

    db.execute = spy
    monkeypatch.setattr(rs, "get_configured_timezone", fake_tz)
    monkeypatch.setattr(rs, "generate_and_deliver", boom)
    asyncio.run(rs.run_scheduler_tick(db))
    assert captured and captured[-1]["st"] == "failed" and captured[-1]["err"] == "RuntimeError"
    assert "ROLLBACK" in db.log


def test_row_without_custom_column_is_tolerated():
    row = _row()
    del row.custom_report_id
    assert rs._row_to_sched(row)["custom_report_id"] is None
    assert isinstance(rs.compute_next_run(rs._row_to_sched(_row()), "UTC",
                                          after=datetime(2026, 10, 7, tzinfo=timezone.utc)), datetime)


def test_row_to_sched_carries_direct_recipients():
    assert rs._row_to_sched(_row(recipients=["ops@example.com"]))["recipients"] == ["ops@example.com"]
    assert rs._row_to_sched(_row())["recipients"] == []          # column absent before migrate-156


# ── Generation: templates through the section engine, delivery ───────────────

import json as _json  # noqa: E402
import struct as _struct  # noqa: E402
import sys as _sys  # noqa: E402
import zlib as _zlib  # noqa: E402
from datetime import timedelta  # noqa: E402

import pytest  # noqa: E402

from app.services import report_periods  # noqa: E402
from app.services import report_sections as _sections  # noqa: E402
from app.services.branding import Branding  # noqa: E402


def _png() -> bytes:
    def chunk(kind, data):
        return _struct.pack(">I", len(data)) + kind + data + _struct.pack(">I", _zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", _struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", _zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


NOW = datetime(2026, 10, 7, 11, 5, tzinfo=timezone.utc)          # Wed 14:05 in Riyadh
COMPANY = {"company_name": "Acme Ops", "timezone": "Asia/Riyadh", "week_start": "sunday",
           "confidentiality": "Confidential", "base_url": "https://nms.acme.example"}
LOGO = ("brand-logo@zenplus", _png(), "image/png")


class _GenResult:
    def __init__(self, rows=None, scalar=None):
        self.rows = rows or []
        self._scalar = scalar

    def first(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def scalar(self):
        return self._scalar


class GenDB:
    """Answers generate_and_deliver's statements; records report_runs writes."""

    def __init__(self, company=None, template=None):
        self.company = dict(COMPANY if company is None else company)
        self.template = template
        self.runs: list[dict] = []
        self.updates: list[dict] = []
        self.report_settings: dict | None = None
        self.channels = {"ch1": SimpleNamespace(id="ch1", name="NOC email", type="email", enabled=True,
                                                gateway_id=None,
                                                config={"recipients": "noc@example.com, cio@example.com"})}

    def begin_nested(self):
        class _N:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False
        return _N()

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        params = dict(params or {})
        if "FROM system_settings WHERE key = ANY" in sql:
            return _GenResult([("company", self.company)])
        if "FROM system_settings WHERE key = :k" in sql:
            return _GenResult([(self.report_settings,)] if self.report_settings else [])
        if sql.startswith("INSERT INTO report_runs"):
            self.runs.append(params)
            return _GenResult([SimpleNamespace(id="run-1")])
        if sql.startswith("UPDATE report_runs"):
            self.updates.append({**params, "d": _json.loads(params["d"]) if "d" in params else None})
            return _GenResult()
        if "FROM notification_channels" in sql:
            ch = self.channels.get(params["id"])
            return _GenResult([ch] if ch else [])
        if "FROM custom_reports c" in sql:
            return _GenResult([self.template] if self.template else [])
        raise AssertionError(f"unexpected SQL: {sql[:100]}")


@pytest.fixture
def gen(monkeypatch):
    seen: dict = {"sent": [], "individual": [], "periods": []}
    real_resolve = report_periods.resolve_period

    def fixed_resolve(period, tz, week_start="monday", now=None):
        seen["periods"].append((period, tz, week_start))
        return real_resolve(period, tz, week_start, now=NOW)

    async def no_owner(db, schedule):
        return seen.get("owner")

    async def fake_build(db, entries, frm, to, filters=None, requester=None, branding=None):
        seen["build"] = dict(entries=entries, frm=frm, to=to, filters=filters, requester=requester,
                             branding=branding)
        return [{"id": "availability_kpis", "title": "Availability",
                 "kpis": [{"label": "Availability", "value": "99.95%"}], "charts": [], "tables": [], "notes": []}]

    def fake_email_html(meta, sections, view_url, attached, logo_cid=None):
        seen["email_html"] = dict(meta=meta, view_url=view_url, attached=attached, logo_cid=logo_cid)
        return "<html>email</html>"

    def fake_render_pdf(meta, sections):
        seen["pdf_meta"] = meta
        return b"%PDF-1.7 fake"

    def fake_send(gw, recipients, subject, text_body, html_body, attachment=None, inline_images=None, **kw):
        seen["sent"].append(dict(recipients=recipients, subject=subject, attachment=attachment,
                                 inline=list(inline_images or []), html=html_body, text=text_body, **kw))

    def fake_individually(gw, recipients, subject, text_body, html_body, attachment=None, inline_images=None,
                          **kw):
        seen["individual"].append(dict(recipients=list(recipients), subject=subject, attachment=attachment,
                                       inline=list(inline_images or []), **kw))
        return list(recipients), []

    async def gateway(db, gateway_id=None):
        return {"host": "smtp.example", "from_email": "reports@acme.example"}

    async def channel_gateway(db, ch):
        return {"host": "smtp.example", "from_email": "reports@acme.example"}

    monkeypatch.setattr(report_periods, "resolve_period", fixed_resolve)
    monkeypatch.setattr(rs, "_schedule_owner", no_owner)
    monkeypatch.setattr(rs, "get_settings", lambda: SimpleNamespace(APP_BASE_URL=""))
    monkeypatch.setattr(_sections, "build_sections", fake_build)
    monkeypatch.setattr(_sections, "render_html", lambda meta, secs: "<html>report</html>")
    monkeypatch.setattr(_sections, "render_pdf", fake_render_pdf)
    monkeypatch.setattr(_sections, "email_summary_html", fake_email_html)
    monkeypatch.setattr(_sections, "email_summary_text", lambda meta, secs, url: "text summary")
    monkeypatch.setattr(_sections, "email_logo_attachment", lambda meta: LOGO, raising=False)
    monkeypatch.setattr(rs, "_send_email_with_attachment", fake_send)
    monkeypatch.setattr(rs, "_send_individually", fake_individually)
    monkeypatch.setattr(rs, "_resolve_smtp_gateway", gateway)
    monkeypatch.setattr(rs, "_resolve_smtp", channel_gateway)
    return seen


def _sched(**kw):
    base = dict(id="s1", name="Monthly SLA", report_type="availability", period="previous_month",
                format="pdf", filters={}, notify_channels=["ch1"],
                recipients=["Ops@Example.com", "ops@example.com", "cto@example.org"], custom_report_id=None)
    base.update(kw)
    return base


def test_template_schedule_renders_like_the_viewer_and_emails_everyone(gen):
    dev = "6f1c1e2a-7d1e-4a51-9a51-3d1c1e2a7d1e"
    db = GenDB()
    out = asyncio.run(rs.generate_and_deliver(db, _sched(filters={"device_ids": [dev], "netflow_withheld": True})))

    assert out["status"] == "success" and out["errors"] == []
    assert out["delivered"] == ["NOC email", "ops@example.com", "cto@example.org"]
    # The window: the previous calendar month in the organisation timezone and week start.
    assert gen["periods"] == [("previous_month", "Asia/Riyadh", "sunday")]
    b = gen["build"]
    assert b["entries"] == _sections.REPORT_PRESETS["availability"]["entries"]
    assert (b["frm"], b["to"]) == (datetime(2026, 8, 31, 21), datetime(2026, 9, 30, 21))   # naive UTC, as /render
    assert b["filters"] == {"device_ids": [dev]} and b["requester"] is None
    assert isinstance(b["branding"], Branding) and b["branding"].display_name == "Acme Ops"
    meta = gen["pdf_meta"]
    assert meta["title"] == "Monthly SLA" and meta["company_name"] == "Acme Ops"
    assert meta["scope_label"] == "1 selected device"

    subject = "[Confidential] Monthly SLA — Previous month (1 Sep 2026 – 30 Sep 2026)"
    channel, = gen["sent"]
    assert channel["recipients"] == ["noc@example.com", "cio@example.com"] and channel["subject"] == subject
    fname, payload, mime = channel["attachment"]
    assert fname.startswith("Acme-Ops-Monthly-SLA-") and fname.endswith(".pdf")
    assert payload == b"%PDF-1.7 fake" and mime == "application/pdf"
    assert channel["inline"] == [LOGO] and channel["html"] == "<html>email</html>"
    direct, = gen["individual"]
    assert direct["recipients"] == ["ops@example.com", "cto@example.org"] and direct["subject"] == subject
    assert direct["attachment"] == channel["attachment"] and direct["inline"] == [LOGO]
    assert gen["email_html"]["logo_cid"] == "brand-logo@zenplus" and gen["email_html"]["attached"] is True
    assert gen["email_html"]["view_url"].startswith("https://nms.acme.example/api/v1/reports/shared/")

    run, = db.runs
    assert run["html"] == "<html>report</html>" and run["rt"] == "availability" and run["pd"] == "previous_month"
    upd, = db.updates
    assert upd["d"] == out["delivered"] and upd["st"] == "success" and upd["err"] is None


@pytest.mark.parametrize("fmt,ext,mime", [
    ("excel", "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("html", "html", "text/html"),
])
def test_template_attachment_formats(gen, monkeypatch, fmt, ext, mime):
    import types
    import app.services as services_pkg

    calls = []
    fake_xlsx = types.ModuleType("app.services.report_xlsx")
    fake_xlsx.render_xlsx = lambda meta, sections: calls.append((meta["title"], sections)) or b"PK\x03\x04xlsx"
    monkeypatch.setitem(_sys.modules, "app.services.report_xlsx", fake_xlsx)
    monkeypatch.setattr(services_pkg, "report_xlsx", fake_xlsx, raising=False)

    out = asyncio.run(rs.generate_and_deliver(GenDB(), _sched(format=fmt)))
    assert out["status"] == "success"
    fname, payload, got_mime = gen["sent"][0]["attachment"]
    assert fname.endswith(f".{ext}") and got_mime == mime
    if fmt == "excel":
        assert payload == b"PK\x03\x04xlsx" and calls and calls[0][0] == "Monthly SLA"
    else:
        assert payload == b"<html>report</html>" and not calls


def test_no_classification_means_no_subject_prefix(gen):
    asyncio.run(rs.generate_and_deliver(GenDB(company={**COMPANY, "confidentiality": ""}),
                                        _sched(recipients=[])))
    assert gen["sent"][0]["subject"] == "Monthly SLA — Previous month (1 Sep 2026 – 30 Sep 2026)"


def test_custom_template_schedule_uses_the_template_entries(gen):
    entries = [{"id": "availability_kpis", "title": "Headline", "options": {}},
               {"id": "top_outages", "title": None, "options": {"limit": 5}}]
    tpl = SimpleNamespace(id="c-123", name="Board SLA", description="For the board", category="executive",
                          sections=["availability_kpis", "top_outages"],
                          definition={"version": 1, "sections": entries}, visibility="shared", owner_id=None)
    sched = _sched(report_type="custom", custom_report_id="3f6c1a52-8b1e-4c7a-9f0d-2a4b6c8d0e1f")
    out = asyncio.run(rs.generate_and_deliver(GenDB(template=tpl), sched))
    assert out["status"] == "success"
    assert [e["id"] for e in gen["build"]["entries"]] == ["availability_kpis", "top_outages"]
    assert gen["build"]["entries"][0]["title"] == "Headline"
    assert gen["build"]["entries"][1]["options"] == {"limit": 5}
    assert gen["pdf_meta"]["category"] == "Executive & SLA" and gen["pdf_meta"]["description"] == "For the board"


@pytest.mark.parametrize("schedule", [
    _sched(report_type="custom", custom_report_id="0b7c5c55-2f0a-4b8e-9d55-6f7b6a0c1d2e"),
    _sched(report_type="custom", custom_report_id=None),
    _sched(report_type="retired_template_key"),
])
def test_a_schedule_whose_template_is_gone_fails_clearly(gen, schedule):
    db = GenDB(template=None)
    out = asyncio.run(rs.generate_and_deliver(db, schedule))
    assert out["status"] == "failed" and out["run_id"] is None and out["delivered"] == []
    assert "nothing was generated" in out["errors"][0]
    assert not db.runs and not gen["sent"] and not gen["individual"] and "build" not in gen


def test_private_template_of_another_user_is_not_run_for_the_owner(gen, monkeypatch):
    tpl = SimpleNamespace(id="c-1", name="Secret", description="", category=None, sections=["availability_kpis"],
                          definition={}, visibility="private", owner_id="someone-else")
    gen["owner"] = SimpleNamespace(id="owner-1", role="rsx_manager", is_active=True)

    async def perms(db, requester):
        return frozenset({"reports.view", "reports.manage"})

    monkeypatch.setattr(_sections, "requester_permissions", perms)
    sched = _sched(report_type="custom", custom_report_id="7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
    out = asyncio.run(rs.generate_and_deliver(GenDB(template=tpl), sched))
    assert out["status"] == "failed" and "private to another user" in out["errors"][0]
    assert "build" not in gen


def test_an_owner_limited_to_a_scope_gets_only_scope_safe_sections(gen, monkeypatch):
    from app.core import scoping
    gen["owner"] = SimpleNamespace(id="owner-1", role="rsx_team", is_active=True)

    async def scoped(db, user, requested):
        return True, ["11111111-1111-1111-1111-111111111111"]

    monkeypatch.setattr(scoping, "scoped_device_filter", scoped)
    out = asyncio.run(rs.generate_and_deliver(GenDB(), _sched(filters={"application_id": "shop"})))
    assert out["status"] == "success"
    ids = [e["id"] for e in gen["build"]["entries"]]
    assert ids and set(ids) <= _sections.SCOPE_SAFE_SECTIONS
    assert len(ids) < len(_sections.REPORT_PRESETS["availability"]["entries"])
    assert gen["build"]["filters"] == {"device_ids": ["11111111-1111-1111-1111-111111111111"]}
    assert gen["build"]["requester"] is gen["owner"]
    assert gen["pdf_meta"]["scope_label"].startswith("Limited to the owner's visibility scope")


def test_partial_delivery_and_attachment_failure(gen, monkeypatch):
    def refuse_one(gw, recipients, *a, **k):
        return recipients[:1], [(r, "550 5.1.1 no such user") for r in recipients[1:]]

    def broken_pdf(meta, sections):
        raise RuntimeError("font missing")

    monkeypatch.setattr(rs, "_send_individually", refuse_one)
    monkeypatch.setattr(_sections, "render_pdf", broken_pdf)
    db = GenDB()
    out = asyncio.run(rs.generate_and_deliver(db, _sched()))
    assert out["status"] == "partial"
    assert out["delivered"] == ["NOC email", "ops@example.com"]
    assert "cto@example.org: 550 5.1.1 no such user" in out["errors"]
    assert any("PDF attachment could not be generated (RuntimeError)" in e for e in out["errors"])
    assert gen["sent"][0]["attachment"] is None and gen["email_html"]["attached"] is False
    assert db.updates[0]["st"] == "partial" and "cto@example.org" in db.updates[0]["err"]


def test_direct_recipients_without_a_gateway_fail_the_run(gen, monkeypatch):
    async def none(db, gateway_id=None):
        return None

    monkeypatch.setattr(rs, "_resolve_smtp_gateway", none)
    out = asyncio.run(rs.generate_and_deliver(GenDB(), _sched(notify_channels=[])))
    assert out["status"] == "failed" and out["delivered"] == []
    assert out["errors"] == ["2 direct recipient(s): no SMTP gateway is configured"]


def test_nothing_to_deliver_is_a_success(gen):
    out = asyncio.run(rs.generate_and_deliver(GenDB(), _sched(notify_channels=[], recipients=[], format="none")))
    assert out["status"] == "success" and out["delivered"] == [] and not gen["individual"]


@pytest.mark.parametrize("tz,period,expect", [
    # A calendar window becomes an explicit range; an exclusive midnight end
    # is passed as the instant before it (the legacy range pushes a midnight
    # end to 23:59:59 of that day).
    ("UTC", "previous_month", ("custom", datetime(2026, 9, 1, tzinfo=timezone.utc),
                               datetime(2026, 10, 1, tzinfo=timezone.utc) - timedelta(microseconds=1))),
    ("Asia/Riyadh", "yesterday", ("custom", datetime(2026, 10, 5, 21, tzinfo=timezone.utc),
                                  datetime(2026, 10, 6, 21, tzinfo=timezone.utc))),
    ("Asia/Riyadh", "last_7d", ("last_7d", None, None)),
])
def test_legacy_types_keep_their_pipeline_on_the_resolved_window(gen, monkeypatch, tz, period, expect):
    seen = {}

    async def fake_executive(db, start, end, visible_tags=None):
        seen["executive"] = (start, end)
        return {"kpis": {"availability_pct": 99.5}}

    async def fake_pdf(**kw):
        seen["pdf"] = kw
        return b"%PDF legacy"

    monkeypatch.setattr(rs, "build_executive", fake_executive)
    monkeypatch.setattr(rs, "generate_report", fake_pdf)
    monkeypatch.setattr(rs.report_html, "build_report_html", lambda meta, data: "<html>legacy</html>")
    monkeypatch.setattr(rs.report_html, "build_report_email_html", lambda *a, **k: "<html>legacy mail</html>")
    monkeypatch.setattr(rs.report_html, "build_report_email_text", lambda *a, **k: "legacy text")
    out = asyncio.run(rs.generate_and_deliver(GenDB(company={**COMPANY, "timezone": tz}),
                                              _sched(report_type="executive_summary", period=period,
                                                     name="Exec", recipients=[])))
    assert out["status"] == "success"
    kw = seen["pdf"]
    assert (kw["period"], kw["from_time"], kw["to_time"]) == expect
    assert kw["redact_netflow"] is False and kw["report_type"] == "executive_summary"
    fname, payload, mime = gen["sent"][0]["attachment"]
    assert fname.startswith("Acme-Ops-Exec-") and fname.endswith(".pdf") and payload == b"%PDF legacy"
    # Legacy reports carry the organisation logo inline, like template reports.
    assert gen["sent"][0]["inline"] == [LOGO] and gen["sent"][0]["subject"].startswith("[Confidential] Exec — ")


# ── Email construction ───────────────────────────────────────────────────────

def test_email_message_has_inline_logo_and_attachment():
    from email import message_from_string

    gw = {"from_name": "Acme Opérations", "from_email": "reports@acme.example"}
    msg = rs.build_email_message(gw, ["a@example.com"], "[Confidential] SLA — Previous month\r\nBcc: x@evil",
                                 "plain", '<img src="cid:brand-logo@zenplus">',
                                 ("Acme-SLA.pdf", b"%PDF", "application/pdf"), [LOGO])
    raw = msg.as_string()
    raw.encode("ascii")                                   # headers are RFC 2047 encoded
    parsed = message_from_string(raw)
    assert "\n" not in parsed["Subject"] and "Bcc" not in parsed.keys()
    assert parsed["From"].endswith("<reports@acme.example>")
    types = [p.get_content_type() for p in parsed.walk()]
    assert types == ["multipart/mixed", "multipart/alternative", "text/plain", "multipart/related",
                     "text/html", "image/png", "application/pdf"]
    img = next(p for p in parsed.walk() if p.get_content_type() == "image/png")
    assert img["Content-ID"] == "<brand-logo@zenplus>" and img.get_payload(decode=True) == LOGO[1]
    pdf = next(p for p in parsed.walk() if p.get_content_type() == "application/pdf")
    assert pdf.get_filename() == "Acme-SLA.pdf"
    html_att = rs.build_email_message(gw, ["a@example.com"], "s", "t", "h",
                                      ("r.html", "<p>é</p>".encode(), "text/html"))
    att = [p for p in html_att.walk() if p.get_filename() == "r.html"][0]
    assert att.get_content_type() == "text/html" and att.get_payload(decode=True).decode() == "<p>é</p>"
    # Without inline images the HTML part sits directly in the alternative.
    plain = rs.build_email_message(gw, ["a@example.com"], "s", "t", "h")
    assert [p.get_content_type() for p in plain.walk()] == ["multipart/mixed", "multipart/alternative",
                                                            "text/plain", "text/html"]


def test_direct_recipients_get_one_message_each(monkeypatch):
    import smtplib
    from email import message_from_string

    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            pass

        def sendmail(self, sender, rcpts, raw):
            if rcpts == ["bad@example.com"]:
                raise smtplib.SMTPRecipientsRefused({"bad@example.com": (550, b"no such user")})
            sent.append((rcpts, raw))

        def quit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(rs.smtplib, "SMTP", FakeSMTP)
    ok, failed = rs._send_individually({"host": "smtp.example", "from_email": "r@acme.example"},
                                       ["a@example.com", "bad@example.com", "c@example.com"],
                                       "Subject", "text", "<p>html</p>")
    assert ok == ["a@example.com", "c@example.com"]
    assert failed == [("bad@example.com", "refused: 550 no such user")]
    for rcpts, raw in sent:
        assert message_from_string(raw)["To"] == rcpts[0]      # nobody sees the other recipients
    assert len({message_from_string(raw)["Message-ID"] for _, raw in sent}) == 2


def test_recipient_normalisation():
    assert rs.normalize_recipients([" A@Example.COM", "a@example.com", "", None, "b@x.io"]) == \
        ["a@example.com", "b@x.io"]
    for bad in ("a@b", "a b@example.com", "a@example.com,b@example.com", "<a@example.com>",
                "a@-example.com", ".a@example.com", "a..b@example.com", "a@example.com\nBcc: c@d.io"):
        with pytest.raises(ValueError):
            rs.normalize_recipients([bad])
    with pytest.raises(ValueError):
        rs.normalize_recipients([f"u{i}@example.com" for i in range(rs.MAX_RECIPIENTS + 1)])



def test_a_dropped_session_keeps_the_recipients_already_sent(monkeypatch):
    sent = []

    class FlakySMTP:
        def __init__(self, host, port, timeout=None):
            pass

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            pass

        def sendmail(self, sender, rcpts, raw):
            if rcpts == ["c@example.com"]:
                raise TimeoutError("timed out")
            sent.append(rcpts[0])

        def quit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(rs.smtplib, "SMTP", FlakySMTP)
    ok, failed = rs._send_individually({"host": "smtp.example", "from_email": "r@acme.example"},
                                       ["a@example.com", "b@example.com", "c@example.com", "d@example.com"],
                                       "Subject", "text", "<p>html</p>")
    assert ok == ["a@example.com", "b@example.com"] == sent
    assert [a for a, _ in failed] == ["c@example.com", "d@example.com"]



def test_tick_claims_one_schedule_at_a_time(monkeypatch):
    """Each run is claimed (and committed) just before it is generated, so a
    worker that dies mid-batch leaves the remaining schedules due."""
    order = []

    async def fake_tz(db):
        return "UTC"

    async def fake_generate(db, sched, **kw):
        claims = [s for s in db.log if s.startswith("UPDATE report_schedules")]
        order.append((sched["id"], len(claims)))
        return {"status": "success", "errors": []}

    monkeypatch.setattr(rs, "get_configured_timezone", fake_tz)
    monkeypatch.setattr(rs, "generate_and_deliver", fake_generate)
    db = TickDB([_row(id="s1", report_type="availability", custom_report_id=None),
                 _row(id="s2", report_type="alerts", custom_report_id=None)])
    out = asyncio.run(rs.run_scheduler_tick(db))
    assert out["fired"] == ["s1", "s2"]
    # s1 was generated after only its own claim; s2 after s1's claim, s1's
    # result and its own claim — never both claimed up front.
    assert order == [("s1", 1), ("s2", 3)]


# ── Schedule timezone (NOTIFY 07) ────────────────────────────────────────────

RIYADH, BERLIN = "Asia/Riyadh", "Europe/Berlin"


def _timing(**kw):
    base = dict(enabled=True, frequency="daily", hour=8, minute=0, day_of_week=None, day_of_month=None,
                timezone=RIYADH)
    base.update(kw)
    return base


def _iso(d):
    return d.isoformat().replace("+00:00", "Z")


def test_daily_0800_riyadh_runs_at_0500_utc_every_day():
    nxt = rs.compute_next_run(_timing(), "UTC", after=NOW)          # Wed 14:05 Riyadh
    assert _iso(nxt) == "2026-10-08T05:00:00Z"
    runs, at = [], NOW
    for _ in range(3):
        at = rs.compute_next_run(_timing(), "UTC", after=at + timedelta(seconds=1))
        runs.append(_iso(at))
    assert runs == ["2026-10-08T05:00:00Z", "2026-10-09T05:00:00Z", "2026-10-10T05:00:00Z"]
    # Just before and exactly at 05:00Z: that day's run, then the next day's.
    assert _iso(rs.compute_next_run(_timing(), "UTC", after=datetime(2026, 10, 8, 4, 59, tzinfo=timezone.utc))) \
        == "2026-10-08T05:00:00Z"
    assert _iso(rs.compute_next_run(_timing(), "UTC", after=datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc))) \
        == "2026-10-09T05:00:00Z"


def test_weekly_sunday_0800_riyadh_runs_only_on_sunday():
    sched = _timing(frequency="weekly", day_of_week=7)
    at, runs = NOW, []
    for _ in range(3):
        at = rs.compute_next_run(sched, "UTC", after=at + timedelta(seconds=1))
        runs.append(at)
    assert [_iso(r) for r in runs] == ["2026-10-11T05:00:00Z", "2026-10-18T05:00:00Z", "2026-10-25T05:00:00Z"]
    assert all(r.astimezone(ZoneInfo(RIYADH)).isoweekday() == 7 for r in runs)
    # Sunday after the run: next Sunday, never Monday.
    assert _iso(rs.compute_next_run(sched, "UTC", after=datetime(2026, 10, 11, 5, 0, 1, tzinfo=timezone.utc))) \
        == "2026-10-18T05:00:00Z"


def test_stored_timezone_wins_and_the_organisation_zone_is_only_a_fallback():
    stored = rs.compute_next_run(_timing(), "Europe/London", after=NOW)
    assert _iso(stored) == "2026-10-08T05:00:00Z"
    # Changing the organisation timezone does not move a schedule with its own zone.
    for org in ("UTC", "America/New_York", "Asia/Tokyo"):
        assert rs.compute_next_run(_timing(), org, after=NOW) == stored
    # Without a (valid) zone of its own, the organisation zone applies.
    for missing in (None, "", "Not/AZone"):
        assert _iso(rs.compute_next_run(_timing(timezone=missing), RIYADH, after=NOW)) == "2026-10-08T05:00:00Z"
        assert _iso(rs.compute_next_run(_timing(timezone=missing), "UTC", after=NOW)) == "2026-10-08T08:00:00Z"
    assert rs.schedule_timezone({"timezone": "  Asia/Riyadh "}, "UTC") == RIYADH
    assert rs.schedule_timezone({}, " Not/AZone ") == "UTC"


def test_dst_zone_keeps_local_time_across_the_clock_change():
    # Europe/Berlin leaves summer time on Sunday 2026-10-25 (03:00 CEST -> 02:00 CET).
    daily = _timing(timezone=BERLIN)
    at, runs = datetime(2026, 10, 23, 12, tzinfo=timezone.utc), []
    for _ in range(4):
        at = rs.compute_next_run(daily, "UTC", after=at + timedelta(seconds=1))
        runs.append(_iso(at))
    assert runs == ["2026-10-24T06:00:00Z", "2026-10-25T07:00:00Z", "2026-10-26T07:00:00Z",
                    "2026-10-27T07:00:00Z"]
    weekly = _timing(timezone=BERLIN, frequency="weekly", day_of_week=7)
    assert _iso(rs.compute_next_run(weekly, "UTC", after=datetime(2026, 10, 18, 12, tzinfo=timezone.utc))) \
        == "2026-10-25T07:00:00Z"
    # Spring forward (2027-03-28): 08:00 CET (07:00Z) becomes 08:00 CEST (06:00Z).
    assert _iso(rs.compute_next_run(weekly, "UTC", after=datetime(2027, 3, 22, tzinfo=timezone.utc))) \
        == "2027-03-28T06:00:00Z"
    assert _iso(rs.compute_next_run(weekly, "UTC", after=datetime(2027, 3, 15, tzinfo=timezone.utc))) \
        == "2027-03-21T07:00:00Z"
    # The label names the zone and the UTC clock of the next run.
    assert rs.local_time_label(daily, "UTC", now=datetime(2026, 10, 23, 12, tzinfo=timezone.utc)) == \
        "08:00 Europe/Berlin (06:00 UTC)"
    assert rs.local_time_label(daily, "UTC", now=datetime(2026, 10, 24, 12, tzinfo=timezone.utc)) == \
        "08:00 Europe/Berlin (07:00 UTC)"


def test_time_labels_and_name_mismatch():
    assert rs.local_time_label(_timing(), "UTC", now=NOW) == "08:00 Asia/Riyadh (05:00 UTC)"
    assert rs.local_time_label(_timing(hour=1), "UTC", now=NOW) == "01:00 Asia/Riyadh (22:00 UTC, previous day)"
    assert rs.local_time_label(_timing(timezone="UTC"), RIYADH, now=NOW) == "08:00 UTC"
    assert rs.schedule_text(_timing(frequency="weekly", day_of_week=7), "UTC", now=NOW) == \
        "Weekly on Sunday at 08:00 Asia/Riyadh (05:00 UTC)"
    assert rs.schedule_text(_timing(frequency="monthly", day_of_month=1), "UTC", now=NOW) == \
        "Monthly on day 1 at 08:00 Asia/Riyadh (05:00 UTC)"
    # The field case: titled 08:00 but stored as 05:00 local.
    assert rs.name_time_mismatch("NOC Daily Briefing 08:00 Riyadh", 5, 0) == "08:00"
    assert rs.name_time_mismatch("NOC Daily Briefing 08:00 Riyadh", 8, 0) is None
    assert rs.name_time_mismatch("Ops digest 7:30 PM", 19, 30) is None
    assert rs.name_time_mismatch("Ops digest 7:30 PM", 7, 30) == "7:30 PM"
    assert rs.name_time_mismatch("Brief 08h30", 8, 0) == "08h30"
    for no_time in ("Weekly SLA", "Release 1.24 notes", "Week of 18.10.2026", "Ratio 3:2:1"):
        assert rs.name_time_mismatch(no_time, 8, 0) is None, no_time


def test_tick_uses_the_schedule_zone_not_the_organisation_zone(monkeypatch):
    claims = []

    async def org_utc(db):
        return "UTC"

    async def fake_generate(db, sched, **kw):
        return {"status": "success", "errors": []}

    db = TickDB([_row(timezone=RIYADH, report_type="availability", custom_report_id=None)])
    orig = db.execute

    async def spy(statement, params=None):
        if params and "nxt" in params:
            claims.append(params)
        return await orig(statement, params)

    db.execute = spy
    monkeypatch.setattr(rs, "get_configured_timezone", org_utc)
    monkeypatch.setattr(rs, "generate_and_deliver", fake_generate)
    asyncio.run(rs.run_scheduler_tick(db))
    nxt = claims[0]["nxt"]
    assert nxt > claims[0]["now"]
    assert nxt.astimezone(ZoneInfo(RIYADH)).strftime("%H:%M") == "08:00"
    assert nxt.strftime("%H:%M") == "05:00"
    assert rs._row_to_sched(_row(timezone=RIYADH))["timezone"] == RIYADH
    assert rs._row_to_sched(_row())["timezone"] is None           # column absent before migrate-163


def test_schedule_runs_label_the_schedule_zone(gen):
    # The organisation moved to Europe/London; the schedule keeps Asia/Riyadh.
    db = GenDB(company={**COMPANY, "timezone": "Europe/London"})
    sched = _sched(id="s1", frequency="daily", hour=8, minute=0, timezone=RIYADH, period="yesterday",
                   name="NOC Daily Briefing", recipients=[])
    out = asyncio.run(rs.generate_and_deliver(db, sched))
    assert out["status"] == "success"
    assert gen["periods"] == [("yesterday", RIYADH, "sunday")]        # window in the schedule zone
    assert gen["build"]["branding"].timezone == RIYADH
    meta = gen["email_html"]["meta"]
    assert meta["timezone"] == RIYADH and "GMT+3" in meta["period_label"]
    assert meta["generated_label"].endswith(" UTC)") and "GMT+3" in meta["generated_label"]
    assert meta["schedule_label"].startswith("Daily at 08:00 Asia/Riyadh (05:00 UTC")
    assert meta["run_kind"] == "scheduled" and "run_note" not in meta
    assert gen["pdf_meta"]["schedule_label"] == meta["schedule_label"]
    assert "[Manual run]" not in gen["sent"][0]["subject"]
    assert db.runs[0]["title"] == "NOC Daily Briefing"


def test_manual_runs_are_labelled_as_manual(gen):
    db = GenDB()
    sched = _sched(frequency="daily", hour=8, minute=0, timezone=RIYADH)
    out = asyncio.run(rs.generate_and_deliver(db, sched, triggered_by="manual"))
    assert out["status"] == "success"
    assert gen["sent"][0]["subject"] == ("[Confidential] [Manual run] Monthly SLA — Previous month "
                                         "(1 Sep 2026 – 30 Sep 2026)")
    assert gen["individual"][0]["subject"] == gen["sent"][0]["subject"]
    meta = gen["email_html"]["meta"]
    assert meta["run_kind"] == "manual" and meta["run_note"] == rs.MANUAL_RUN_NOTE
    assert db.runs[0]["title"] == "Monthly SLA (manual run)"            # run history shows it too


def test_a_restarted_scheduler_runs_a_missed_schedule_once(monkeypatch):
    """Down for three days: the overdue schedule fires once on the first tick
    and is claimed forward to its next local 08:00, not replayed per missed day."""
    rows = {"s1": dict(id="s1", name="Daily", enabled=True, report_type="availability", period="yesterday",
                       format="pdf", filters={}, frequency="daily", hour=8, minute=0, day_of_week=None,
                       day_of_month=None, notify_channels=[], custom_report_id=None, timezone=RIYADH,
                       next_run_at=datetime(2026, 10, 4, 5, tzinfo=timezone.utc))}
    fired = []

    class StateDB:
        async def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            params = params or {}

            class R:
                def __init__(self, row=None):
                    self.row = row

                def first(self):
                    return self.row

            if sql.startswith("SELECT * FROM report_schedules"):
                due = [r for r in rows.values() if r["enabled"] and r["next_run_at"] <= params["now"]
                       and r["id"] not in params["done"]]
                return R(SimpleNamespace(**due[0]) if due else None)
            if "next_run_at = :nxt" in sql:
                rows[params["id"]]["next_run_at"] = params["nxt"]
            return R()

        async def commit(self):
            pass

        async def rollback(self):
            pass

    async def org(db):
        return "UTC"

    async def gen_(db, sched, **kw):
        fired.append(sched["id"])
        return {"status": "success", "errors": []}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 7, 11, 5, tzinfo=timezone.utc)

    monkeypatch.setattr(rs, "datetime", Clock)
    monkeypatch.setattr(rs, "get_configured_timezone", org)
    monkeypatch.setattr(rs, "generate_and_deliver", gen_)
    db = StateDB()
    asyncio.run(rs.run_scheduler_tick(db))
    asyncio.run(rs.run_scheduler_tick(db))           # a second worker / the next tick
    assert fired == ["s1"]
    assert _iso(rows["s1"]["next_run_at"]) == "2026-10-08T05:00:00Z"


# ── Delivery after the SMASCO assessment (docs/reports-delivery.md) ──────────

def _no_data_build(gen, monkeypatch):
    async def empty_build(db, entries, frm, to, filters=None, requester=None, branding=None):
        gen["build"] = dict(entries=entries, filters=filters)
        return [{"id": "availability_kpis", "title": "Availability",
                 "kpis": [{"label": "Availability", "value": "—"}], "charts": [], "tables": [], "notes": []}]

    monkeypatch.setattr(_sections, "build_sections", empty_build)


def test_a_run_starts_as_running_and_expires_its_link(gen):
    db = GenDB()
    db.report_settings = {"link_expiry_days": 7}
    asyncio.run(rs.generate_and_deliver(db, _sched()))
    run, = db.runs
    assert run["exp"] == 7 and run["kind"] == "scheduled"
    assert run["wf"] < run["wt"]
    upd, = db.updates
    assert upd["subj"].startswith("[Confidential] Monthly SLA") and upd["eh"] == "<html>email</html>"
    assert upd["art"] == b"%PDF-1.7 fake" and upd["am"] == "application/pdf"


def test_no_base_url_says_how_to_find_the_report(gen):
    asyncio.run(rs.generate_and_deliver(GenDB(company={**COMPANY, "base_url": ""}), _sched()))
    assert gen["email_html"]["view_url"] is None
    assert gen["email_html"]["meta"]["delivery_note"] == rs.NO_LINK_NOTE


def test_an_attachment_over_the_limit_is_left_out(gen, monkeypatch):
    monkeypatch.setattr(_sections, "render_pdf", lambda meta, secs: b"x" * (2 * 1024 * 1024))
    db = GenDB()
    db.report_settings = {"max_attachment_mb": 1}
    out = asyncio.run(rs.generate_and_deliver(db, _sched()))
    assert out["status"] == "partial"
    assert gen["sent"][0]["attachment"] is None and gen["email_html"]["attached"] is False
    assert "over the 1 MB limit" in gen["email_html"]["meta"]["delivery_note"]


def test_an_empty_report_is_marked_or_skipped(gen, monkeypatch):
    _no_data_build(gen, monkeypatch)
    out = asyncio.run(rs.generate_and_deliver(GenDB(), _sched(recipients=[])))
    assert out["status"] == "success" and gen["sent"][0]["subject"].startswith("[Confidential] [No data] ")
    assert rs.NO_DATA_NOTE in out["notes"] and rs.NO_DATA_NOTE not in out["errors"]
    gen["sent"].clear()
    db = GenDB()
    out = asyncio.run(rs.generate_and_deliver(db, _sched(skip_when_empty=True)))
    assert out["status"] == "skipped" and not gen["sent"] and not gen["individual"]
    assert db.updates[-1]["n"] and "not sent" in db.updates[-1]["n"]


def test_a_failed_section_makes_the_run_partial(gen, monkeypatch):
    async def build(db, entries, frm, to, filters=None, requester=None, branding=None):
        return [{"id": "availability_kpis", "title": "Availability", "kpis": [{"label": "A", "value": "99%"}],
                 "charts": [], "tables": [], "notes": []},
                {"id": "server_top_processes", "title": "Top Processes", "error": True,
                 "notes": ["Metrics for this section are temporarily unavailable"]}]

    monkeypatch.setattr(_sections, "build_sections", build)
    out = asyncio.run(rs.generate_and_deliver(GenDB(), _sched()))
    assert out["status"] == "partial"
    assert any("Top Processes" in e for e in out["errors"])


def test_subject_template_ignores_format_specs():
    now = datetime(2026, 10, 7, 5, tzinfo=timezone.utc)
    out = rs.render_subject_template("{title:>999999999} {title} {period!r} {date}", title="T",
                                     period_label="P", generated=now, tz="Asia/Riyadh")
    assert out == "{title:>999999999} T {period!r} 2026-10-07"


def test_subject_template_reply_to_and_group_send(gen):
    out = asyncio.run(rs.generate_and_deliver(GenDB(), _sched(
        notify_channels=[], subject_template="{title} | {period} | {date} | {unknown}",
        reply_to="noc@example.com", group_send=True, cc=["boss@example.com"])))
    assert out["status"] == "success"
    sent, = gen["sent"]
    assert sent["subject"].startswith("[Confidential] Monthly SLA | Previous month (1 Sep 2026 – 30 Sep 2026) | ")
    assert sent["subject"].endswith(" | {unknown}")
    assert sent["recipients"] == ["ops@example.com", "cto@example.org"] and sent["cc"] == ["boss@example.com"]
    assert sent["reply_to"] == "noc@example.com" and not gen["individual"]
    assert out["delivered"] == ["ops@example.com", "cto@example.org", "boss@example.com"]


def test_temporary_failures_are_retried_and_permanent_ones_are_not(gen, monkeypatch):
    def flaky(gw, recipients, *a, **k):
        return [], [("ops@example.com", "451 4.7.1 try again later"), ("cto@example.org", "550 5.1.1 no user")]

    monkeypatch.setattr(rs, "_send_individually", flaky)
    db = GenDB()
    out = asyncio.run(rs.generate_and_deliver(db, _sched(notify_channels=[])))
    assert out["status"] == "failed" and out["retry_at"]
    ft = _json.loads(db.updates[-1]["ft"])
    assert [t["id"] for t in ft] == ["ops@example.com"]
    assert db.updates[-1]["nr"] is not None


def test_csv_attachment_for_templates(gen, monkeypatch):
    import types
    import app.services as services_pkg
    fake = types.ModuleType("app.services.report_csv")
    fake.render_csv = lambda meta, secs: b"\xef\xbb\xbfReport,x"
    monkeypatch.setitem(_sys.modules, "app.services.report_csv", fake)
    monkeypatch.setattr(services_pkg, "report_csv", fake, raising=False)
    asyncio.run(rs.generate_and_deliver(GenDB(), _sched(format="csv")))
    fname, payload, mime = gen["sent"][0]["attachment"]
    assert fname.endswith(".csv") and mime == "text/csv" and payload.startswith(b"\xef\xbb\xbf")


def test_short_error_hides_sql_and_unknown_messages():
    from sqlalchemy.exc import DBAPIError

    class Orig(Exception):
        pass

    exc = DBAPIError("SELECT 1", {"id": "x"}, Orig("invalid input for query argument $1: 'x'"))
    msg = rs.short_error(exc)
    assert "SELECT" not in msg and "sqlalche.me" not in msg and "invalid input" in msg
    assert rs.short_error(RuntimeError("password=secret")) == "RuntimeError"
    assert rs._retryable("451 4.7.1 later") and not rs._retryable("refused: 550 5.1.1 no user")
    assert rs._retryable("TimeoutError: timed out")


def test_suggested_time_from_the_name():
    assert rs.suggested_time("Daily NOC - 08:00 Riyadh", 5, 0) == {"hour": 8, "minute": 0}
    assert rs.suggested_time("Daily NOC - 08:00 Riyadh", 8, 0) is None
    assert rs.suggested_time("Digest 7:30 PM", 7, 30) == {"hour": 19, "minute": 30}
    assert rs.suggested_time(None, 8, 0) is None


@pytest.mark.parametrize("enc,offers,expect_tls", [
    ("none", False, False),     # a plain relay on port 25: never forced into STARTTLS
    ("none", True, True),       # gateways saved before 'encryption' existed relied on STARTTLS (O365 on 587)
    ("tls", True, True),
    ("tls", False, True),       # required: the server's refusal surfaces as an error
])
def test_starttls_policy(monkeypatch, enc, offers, expect_tls):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls.append(("connect", port))

        def ehlo_or_helo_if_needed(self):
            calls.append("ehlo")

        def has_extn(self, name):
            return offers and name.lower() == "starttls"

        def starttls(self, context=None):
            calls.append("starttls")

        def login(self, *a):
            calls.append("login")

        def close(self):
            pass

    monkeypatch.setattr(rs.smtplib, "SMTP", FakeSMTP)
    rs._smtp_connect({"host": "relay", "port": 25, "encryption": enc})
    assert ("starttls" in calls) is expect_tls


def test_group_message_carries_cc_and_reply_to():
    from email import message_from_string
    msg = rs.build_email_message({"from_email": "r@acme.example"}, ["a@example.com"], "S", "t", "<p>h</p>",
                                 cc=["b@example.com", "Bad <x@y>"], reply_to="noc@example.com")
    parsed = message_from_string(msg.as_string())
    assert parsed["Cc"] == "b@example.com" and parsed["Reply-To"] == "noc@example.com"


class RetryDB:
    """A stored run that still owes two targets."""

    def __init__(self, failed, email_html="<html>stored</html>", locked=True):
        self.locked = locked
        self.run = SimpleNamespace(
            id="r1", schedule_id="s1", title="Monthly SLA", token="tok", status="failed",
            delivered_to=[], failed_targets=failed, retry_count=0, email_subject="[Confidential] Monthly SLA",
            email_html=email_html, email_text="stored text", artifact=b"%PDF", artifact_name="a.pdf",
            artifact_mime="application/pdf", window_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
            window_to=datetime(2026, 10, 1, tzinfo=timezone.utc), notes=[], error="x",
            reply_to=None, group_send=False, cc=[], schedule_name="Monthly SLA", timezone="Asia/Riyadh")
        self.updates: list[dict] = []

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        params = dict(params or {})
        if "FROM report_runs r LEFT JOIN report_schedules" in sql:
            return _GenResult([self.run])
        if "pg_try_advisory_xact_lock" in sql:
            return _GenResult(scalar=self.locked)
        if "FROM system_settings WHERE key = ANY" in sql:
            return _GenResult([("company", COMPANY)])
        if "FROM system_settings WHERE key = :k" in sql:
            return _GenResult()
        if sql.startswith("UPDATE report_runs") or sql.startswith("UPDATE report_schedules"):
            self.updates.append({"sql": sql, **params})
            return _GenResult()
        raise AssertionError(f"unexpected SQL: {sql[:100]}")

    async def commit(self):
        return None

    async def rollback(self):
        return None


def test_retry_resends_the_stored_email_to_the_owed_targets(gen):
    db = RetryDB([{"kind": "email", "id": "ops@example.com", "name": "ops@example.com", "error": "451"}])
    out = asyncio.run(rs.retry_run(db, "r1"))
    assert out["status"] == "success" and out["remaining"] == 0
    sent, = gen["individual"]
    assert sent["recipients"] == ["ops@example.com"] and sent["subject"] == "[Confidential] Monthly SLA"
    assert sent["attachment"] == ("a.pdf", b"%PDF", "application/pdf")
    run_upd = next(u for u in db.updates if u["sql"].startswith("UPDATE report_runs"))
    assert run_upd["rc"] == 1 and run_upd["nr"] is None and _json.loads(run_upd["ft"]) == []


def test_retry_gives_up_after_the_configured_attempts(gen, monkeypatch):
    def still_down(gw, recipients, *a, **k):
        return [], [(r, "421 service not available") for r in recipients]

    monkeypatch.setattr(rs, "_send_individually", still_down)
    db = RetryDB([{"kind": "email", "id": "ops@example.com", "name": "ops@example.com", "error": "421"}])
    db.run.retry_count = 2                               # the third (last default) attempt
    out = asyncio.run(rs.retry_run(db, "r1"))
    assert out["remaining"] == 1 and out["next_retry_at"] is None
    run_upd = next(u for u in db.updates if u["sql"].startswith("UPDATE report_runs"))
    assert any("gave up" in n for n in _json.loads(run_upd["n"]))


def test_retry_without_a_stored_email_stops(gen):
    db = RetryDB([{"kind": "email", "id": "ops@example.com", "name": "ops@example.com", "error": "451"}],
                 email_html=None)
    out = asyncio.run(rs.retry_run(db, "r1"))
    assert out["remaining"] == 0 and "retention" in out["errors"][0]
    assert not gen["individual"]


def test_retry_is_refused_while_another_is_in_progress(gen):
    db = RetryDB([{"kind": "email", "id": "ops@example.com", "name": "ops@example.com", "error": "451"}],
                 locked=False)
    out = asyncio.run(rs.retry_run(db, "r1"))
    assert out["status"] == "busy" and not gen["individual"] and not db.updates


def test_a_successful_retry_keeps_the_permanent_failures(gen, monkeypatch):
    def first(gw, recipients, *a, **k):
        return [], [("ops@example.com", "451 later"), ("cto@example.org", "550 5.1.1 no user")]

    monkeypatch.setattr(rs, "_send_individually", first)
    db = GenDB()
    asyncio.run(rs.generate_and_deliver(db, _sched(notify_channels=[])))
    notes = _json.loads(db.updates[-1]["notes"])
    assert any(n.startswith(rs.NOT_DELIVERED_PREFIX) and "cto@example.org" in n for n in notes)
    # The retry then delivers ops@: the run stays partial and still names cto@.
    retry_db = RetryDB([{"kind": "email", "id": "ops@example.com", "name": "ops@example.com", "error": "451"}])
    retry_db.run.notes = notes
    monkeypatch.setattr(rs, "_send_individually", lambda gw, r, *a, **k: (list(r), []))
    out = asyncio.run(rs.retry_run(retry_db, "r1"))
    assert out["status"] == "partial" and out["remaining"] == 0
    upd = next(u for u in retry_db.updates if u["sql"].startswith("UPDATE report_runs"))
    assert "cto@example.org" in upd["err"]


@pytest.mark.parametrize("code,retried", [(503, True), (429, True), (404, False), (401, False)])
def test_webhook_failures_are_retried_by_http_status(gen, monkeypatch, code, retried):
    from app.api.v1 import alert_engine

    class HTTPError(Exception):
        response = SimpleNamespace(status_code=code)

    async def result(ch_type, cfg, ctx):
        return False, HTTPError("boom")

    monkeypatch.setattr(alert_engine, "_dispatch_channel_result", result)
    db = GenDB()
    db.channels["hook"] = SimpleNamespace(id="hook", name="Teams NOC", type="teams", enabled=True,
                                          gateway_id=None, config={"url": "https://hooks.example/x"})
    out = asyncio.run(rs.generate_and_deliver(db, _sched(notify_channels=["hook"], recipients=[])))
    assert out["status"] == "failed" and f"Teams NOC: HTTP {code}" in out["errors"]
    ft = _json.loads(db.updates[-1]["ft"])
    assert bool(ft) is retried


def test_a_scoped_owner_s_legacy_attachment_is_confined(gen, monkeypatch):
    from app.core import scoping
    gen["owner"] = SimpleNamespace(id="owner-1", role="rsx_team", is_active=True)
    seen = {}

    async def scoped(db, user, requested):
        return True, ["11111111-1111-1111-1111-111111111111"]

    async def tags(db, user):
        return ["team-a"]

    async def fake_executive(db, start, end, visible_tags=None):
        seen["tags"] = visible_tags
        return {"kpis": {}}

    async def fake_pdf(**kw):
        seen["pdf"] = kw
        return b"%PDF"

    async def flows(db, owner):
        return SimpleNamespace(can=lambda perm: True)

    from app.services import netflow_report
    monkeypatch.setattr(netflow_report, "flow_visibility_for", flows)
    monkeypatch.setattr(scoping, "scoped_device_filter", scoped)
    monkeypatch.setattr(scoping, "visible_tags", tags)
    monkeypatch.setattr(rs, "build_executive", fake_executive)
    monkeypatch.setattr(rs, "generate_report", fake_pdf)
    monkeypatch.setattr(rs.report_html, "build_report_html", lambda meta, data: "<html>legacy</html>")
    monkeypatch.setattr(rs.report_html, "build_report_email_html", lambda *a, **k: "<html>m</html>")
    monkeypatch.setattr(rs.report_html, "build_report_email_text", lambda *a, **k: "t")
    out = asyncio.run(rs.generate_and_deliver(GenDB(), _sched(
        report_type="executive_summary", recipients=[], filters={"locations": ["Jeddah"]})))
    assert out["status"] == "success" and seen["tags"] == ["team-a"]
    assert seen["pdf"]["device_ids"] == ["11111111-1111-1111-1111-111111111111"]
    assert seen["pdf"]["locations"] is None
    out = asyncio.run(rs.generate_and_deliver(GenDB(), _sched(report_type="full_report", recipients=[])))
    assert out["status"] == "failed" and "visibility scope" in out["errors"][0]
