"""Scheduler tick: a due schedule is claimed (next_run_at advanced and
committed) before generation, and timer-fired custom schedules keep their
custom_report_id."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

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
        if sql.startswith("INSERT INTO report_runs"):
            self.runs.append(params)
            return _GenResult([SimpleNamespace(id="run-1")])
        if sql.startswith("UPDATE report_runs"):
            self.updates.append({**params, "d": _json.loads(params["d"])})
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

    def fake_send(gw, recipients, subject, text_body, html_body, attachment=None, inline_images=None):
        seen["sent"].append(dict(recipients=recipients, subject=subject, attachment=attachment,
                                 inline=list(inline_images or []), html=html_body, text=text_body))

    def fake_individually(gw, recipients, subject, text_body, html_body, attachment=None, inline_images=None):
        seen["individual"].append(dict(recipients=list(recipients), subject=subject, attachment=attachment,
                                       inline=list(inline_images or [])))
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
    assert meta["scope_label"] == "1 selected device(s)"

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
