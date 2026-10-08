"""Organisation branding: logo validation, sanitised settings and the
regional formatting every report renderer shares (services/branding.py), plus
the Settings → Company endpoints that store them."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1.settings import CompanySettings
from app.core.database import get_db
from app.core.security import get_current_user
from app.main import app
from app.services import branding

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'


# ── logo sniffing ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("data,mime", [
    (PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp"), (SVG, "image/svg+xml"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "image/svg+xml"),
    # Same-document references stay allowed (gradients, reused shapes).
    (b'<svg xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="g"/></defs>'
     b'<rect fill="url(#g)"/><use href="#g"/><use xlink:href=\'#g\'/></svg>', "image/svg+xml"),
])
def test_sniff_accepts_supported_images(data, mime):
    assert branding.sniff_logo(data) == mime


@pytest.mark.parametrize("data", [
    b"",
    b"GIF89a....",
    b"%PDF-1.7",
    b"<html><body>hi</body></html>",
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><div/></foreignObject></svg>',
    # Anything that makes the PDF renderer load another resource (SSRF / file read).
    b'<svg xmlns="http://www.w3.org/2000/svg"><image href="http://169.254.169.254/x.png"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><image xlink:href="/etc/passwd"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><use href="/var/lib/x.svg#a"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><use xlink:href=file:///etc/hosts /></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(http://x/y)"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(x.css)</style></svg>',
    b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]><svg xmlns="http://www.w3.org/2000/svg"/>',
])
def test_sniff_rejects_other_or_active_content(data):
    assert branding.sniff_logo(data) is None


def test_decode_logo_trusts_bytes_not_stored_type():
    stored = {"data": base64.b64encode(PNG).decode(), "content_type": "image/svg+xml"}
    logo = branding.decode_logo(stored)
    assert logo is not None and logo.content_type == "image/png"
    assert logo.data_uri.startswith("data:image/png;base64,")
    # Legacy rows held a bare (or data-URI) base64 string.
    assert branding.decode_logo("data:image/png;base64," + base64.b64encode(PNG).decode()).data == PNG
    # A stored SVG that would run script is dropped rather than rendered.
    bad = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg" onload="x()"/>').decode()
    assert branding.decode_logo({"data": bad}) is None
    assert branding.decode_logo({"data": "%%%not-base64"}) is None


# ── sanitising stored values ────────────────────────────────────────────────

def test_branding_defaults_and_sanitising():
    b = branding.branding_from_values(None)
    assert b.display_name == "ZenPlus" and b.timezone == "UTC" and b.brand_color == "#4F6BF6"

    b = branding.branding_from_values({
        "company_name": "  Acme Corp  ", "timezone": "Mars/Olympus", "date_format": "%d",
        "time_format": "25h", "brand_color": "red", "confidentiality": "Top Secret",
        "paper_size": "A3", "week_start": "Friday", "company_email": 42,
    })
    assert b.display_name == "Acme Corp"
    assert (b.timezone, b.date_format, b.time_format) == ("UTC", "YYYY-MM-DD", "24h")
    assert (b.brand_color, b.confidentiality, b.paper_size, b.week_start) == ("#4F6BF6", "", "A4", "monday")
    assert b.company_email == ""

    b = branding.branding_from_values({"brand_color": "#0a7c5a", "week_start": "SUNDAY"})
    assert b.brand_color == "#0A7C5A" and b.brand_rgb == (10, 124, 90) and b.week_start == "sunday"


# ── regional formatting ─────────────────────────────────────────────────────

TS = datetime(2026, 10, 7, 11, 5, 9, tzinfo=timezone.utc)


@pytest.mark.parametrize("fmt,expected", [
    ("YYYY-MM-DD", "2026-10-07"), ("DD/MM/YYYY", "07/10/2026"), ("MM/DD/YYYY", "10/07/2026"),
    ("DD.MM.YYYY", "07.10.2026"), ("DD-MMM-YYYY", "07-Oct-2026"), ("MMM D, YYYY", "Oct 7, 2026"),
])
def test_date_formats(fmt, expected):
    assert branding.branding_from_values({"date_format": fmt}).fmt_date(TS) == expected
    assert branding.DATE_FORMATS[fmt] == expected


def test_times_render_in_the_organisation_timezone():
    riyadh = branding.branding_from_values({"timezone": "Asia/Riyadh"})
    assert riyadh.fmt_time(TS) == "14:05"
    assert riyadh.tz_label(TS) == "GMT+3"
    # Naive datetimes are UTC (the database convention).
    assert riyadh.fmt_datetime(TS.replace(tzinfo=None)) == "2026-10-07 14:05"
    assert riyadh.fmt_datetime(TS, with_tz=True) == "2026-10-07 14:05 GMT+3"

    ny = branding.branding_from_values({"timezone": "America/New_York", "time_format": "12h",
                                        "date_format": "MM/DD/YYYY"})
    assert ny.fmt_time(TS) == "7:05 AM" and ny.tz_label(TS) == "GMT-4"
    assert ny.fmt_time(TS, seconds=True) == "7:05:09 AM"
    assert ny.period_label(TS, TS.replace(hour=23)) == "10/07/2026 7:05 AM — 10/07/2026 7:05 PM GMT-4"
    assert ny.tz_label(datetime(2026, 1, 7, tzinfo=timezone.utc)) == "GMT-5"

    kolkata = branding.branding_from_values({"timezone": "Asia/Kolkata"})
    assert kolkata.tz_label(TS) == "GMT+5:30"
    london = branding.branding_from_values({"timezone": "Europe/London"})
    assert london.tz_label(datetime(2026, 1, 7, tzinfo=timezone.utc)) == "GMT"
    assert branding.branding_from_values({"timezone": "Asia/Kathmandu"}).tz_label(TS) == "GMT+5:45"
    assert branding.branding_from_values({}).tz_label(TS) == "UTC"


def test_public_dict_has_no_logo_bytes():
    b = branding.branding_from_values({"company_name": "Acme"},
                                      logo={"data": base64.b64encode(PNG).decode(), "updated_at": "v1"})
    d = b.public_dict()
    assert d["has_logo"] is True and d["has_logo_dark"] is False and d["logo_version"] == "v1"
    assert "data" not in json.dumps(d).lower().replace("date_format", "")
    assert b.cover_logo() is b.logo


# ── settings model validation ───────────────────────────────────────────────

def test_company_settings_validation():
    ok = CompanySettings(timezone="Asia/Riyadh", brand_color="#abcdef", date_format="DD-MMM-YYYY",
                         time_format="12h", confidentiality="Confidential", paper_size="Letter",
                         week_start="Sunday", company_name="  Acme ")
    assert ok.brand_color == "#ABCDEF" and ok.week_start == "sunday" and ok.company_name == "Acme"
    assert CompanySettings(base_url=" https://nms.example.com/ ").base_url == "https://nms.example.com"
    for bad in ({"timezone": "Nowhere/City"}, {"brand_color": "#12345"}, {"date_format": "YY"},
                {"time_format": "am"}, {"confidentiality": "Secret"}, {"paper_size": "A5"},
                {"report_footer": "x" * 201}, {"base_url": "ftp://x"}):
        with pytest.raises(ValidationError):
            CompanySettings(**bad)


# ── endpoints ───────────────────────────────────────────────────────────────

class SettingsDB:
    """system_settings as a dict; enough SQL for the company/logo routes."""
    answers_role_lookup = False

    def __init__(self, store):
        self.store = store

    def begin_nested(self):
        from tests.conftest import _NullTransaction
        return _NullTransaction()

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        p = params or {}

        class R:
            def __init__(self, rows=(), rowcount=0):
                self.rows, self.rowcount = list(rows), rowcount

            def first(self):
                return self.rows[0] if self.rows else None

            def fetchall(self):
                return self.rows

        if sql.startswith("SELECT value FROM system_settings"):
            v = self.store.get(p["key"])
            return R([(v,)] if v is not None else [])
        if sql.startswith("SELECT key, value FROM system_settings"):
            return R([(k, self.store[k]) for k in p["keys"] if k in self.store])
        if sql.startswith("INSERT INTO system_settings"):
            self.store[p["key"]] = json.loads(p["value"])
            return R(rowcount=1)
        if sql.startswith("DELETE FROM system_settings"):
            return R(rowcount=1 if self.store.pop(p["key"], None) is not None else 0)
        return R()

    async def commit(self):
        return None

    async def rollback(self):
        return None


@pytest.fixture
def api():
    from tests.conftest import make_user
    store: dict = {}

    async def fake_db():
        yield SettingsDB(store)

    async def admin():
        return make_user("admin")

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = admin
    yield TestClient(app), store
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def test_logo_upload_sniffs_type_and_serves_sandboxed(api):
    client, store = api
    r = client.post("/api/v1/settings/company/logo",
                    files={"file": ("logo.png", b"<html>no</html>", "image/png")})
    assert r.status_code == 400

    r = client.post("/api/v1/settings/company/logo?variant=dark",
                    files={"file": ("logo.svg", SVG, "application/octet-stream")})
    assert r.status_code == 200, r.text
    assert r.json()["content_type"] == "image/svg+xml"
    assert store["company_logo_dark"]["content_type"] == "image/svg+xml"
    assert store["company_logo_dark"]["updated_at"]

    # Light falls back to the dark variant until one is uploaded.
    r = client.get("/api/v1/settings/company/logo")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/v1/settings/company/logo?fallback=false").status_code == 404
    assert client.get("/api/v1/settings/company/logo?variant=blue").status_code == 400

    assert client.delete("/api/v1/settings/company/logo?variant=dark").status_code == 200
    assert client.delete("/api/v1/settings/company/logo?variant=dark").status_code == 404
    assert client.get("/api/v1/settings/company/logo").status_code == 404


def test_logo_upload_size_limit(api):
    client, _ = api
    big = PNG + b"\x00" * branding.LOGO_MAX_BYTES
    r = client.post("/api/v1/settings/company/logo", files={"file": ("big.png", big, "image/png")})
    assert r.status_code == 400 and "2 MB" in r.json()["detail"]


def test_company_round_trip_and_branding_endpoint(api):
    client, store = api
    store["company"] = {"company_name": "Old", "timezone": "Bad/Zone", "future_key": "kept",
                        "base_url": "https://nms.example.com"}
    got = client.get("/api/v1/settings/company").json()
    assert got["timezone"] == "UTC" and got["brand_color"] == "#4F6BF6"
    # Saving the form round-trips the share-link base URL instead of wiping it.
    assert got["base_url"] == "https://nms.example.com"
    assert got.pop("logos") == {"light": None, "dark": None}

    body = {**got, "company_name": "Acme NOC", "timezone": "Asia/Riyadh", "brand_color": "#0f766e",
            "time_format": "12h", "confidentiality": "Internal", "report_footer": "Acme · NOC"}
    r = client.put("/api/v1/settings/company", json=body)
    assert r.status_code == 200, r.text
    assert store["company"]["future_key"] == "kept"
    assert store["company"]["brand_color"] == "#0F766E"

    assert client.put("/api/v1/settings/company", json={**body, "timezone": "X/Y"}).status_code == 422

    store["company_logo"] = {"data": base64.b64encode(PNG).decode(), "updated_at": "2026-10-07",
                             "filename": "acme.png"}
    logos = client.get("/api/v1/settings/company").json()["logos"]
    assert logos["light"] == {"content_type": "image/png", "filename": "acme.png",
                              "size": len(PNG), "updated_at": "2026-10-07"}
    assert logos["dark"] is None
    b = client.get("/api/v1/reports/branding").json()
    assert b["display_name"] == "Acme NOC" and b["timezone"] == "Asia/Riyadh"
    assert b["has_logo"] is True and b["logo_version"] == "2026-10-07"
    assert b["confidentiality"] == "Internal" and b["report_footer"] == "Acme · NOC"


def test_branding_endpoint_needs_reports_view_not_settings(api):
    client, _ = api
    from tests.conftest import make_user

    async def viewer():
        return make_user("viewer")
    app.dependency_overrides[get_current_user] = viewer
    assert client.get("/api/v1/reports/branding").status_code == 200
    assert client.put("/api/v1/settings/company", json={}).status_code == 403
    assert client.post("/api/v1/settings/company/logo",
                       files={"file": ("l.png", PNG, "image/png")}).status_code == 403
