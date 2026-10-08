"""Organisation branding and regional formatting shared by every report output.

Settings → General → Company stores the organisation profile in
``system_settings['company']`` and its logos in ``company_logo`` (for light
backgrounds: report pages, printed output) and ``company_logo_dark`` (for dark
backgrounds: the report cover). This module is the single reader of those keys
for reports, the scheduler and emails, so every renderer prints the same name,
logo, colour, timezone and date/time format.

Stored values are sanitised on read: an unknown timezone, format or colour
falls back to its default instead of failing a report.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone as _utc_tz
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_BRAND_COLOR = "#4F6BF6"
PRODUCT_NAME = "ZenPlus"

# Date format keys (as stored) -> strftime-free renderers. Keys are the
# user-visible patterns so the stored value stays human-readable.
DATE_FORMATS: dict[str, str] = {
    "YYYY-MM-DD": "2026-10-07",
    "DD/MM/YYYY": "07/10/2026",
    "MM/DD/YYYY": "10/07/2026",
    "DD.MM.YYYY": "07.10.2026",
    "DD-MMM-YYYY": "07-Oct-2026",
    "MMM D, YYYY": "Oct 7, 2026",
}
TIME_FORMATS = ("24h", "12h")
CONFIDENTIALITY_LEVELS = ("", "Public", "Internal", "Confidential", "Restricted")
PAPER_SIZES = ("A4", "Letter")
WEEK_STARTS = ("monday", "sunday", "saturday")

LOGO_KEYS = {"light": "company_logo", "dark": "company_logo_dark"}
LOGO_MAX_BYTES = 2 * 1024 * 1024

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def valid_timezone(name: str) -> bool:
    if not name or not isinstance(name, str):
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def valid_base_url(value: str) -> bool:
    return isinstance(value, str) and bool(re.match(r"^https?://[^\s/$.?#][^\s]*$", value, re.IGNORECASE))


def valid_hex_color(value: str) -> bool:
    return isinstance(value, str) and bool(_HEX_RE.match(value))


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value if valid_hex_color(value) else DEFAULT_BRAND_COLOR
    return int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16)


# ─── Logo validation ────────────────────────────────────────────────────────

# Active content, and anything that makes a renderer load another resource:
# fpdf2 follows <image href> / <use href> / CSS url() to local files and URLs
# when it draws the logo into a PDF (file disclosure, SSRF), and DTD entities
# can expand without bound. Only same-document references (#id) are allowed.
_SVG_FORBIDDEN = re.compile(
    rb"<\s*script|<\s*foreignObject|\bon[a-z]+\s*=|javascript:|<\s*iframe|<\s*embed|<\s*object"
    rb"|<\s*image\b|<\s*style\b|<\s*!DOCTYPE|<\s*!ENTITY|@import"
    rb"""|\bhref\s*=\s*(?:"\s*(?!#)|'\s*(?!#)|(?!["'\s#]))"""
    rb"""|url\(\s*(?:"\s*(?!#)|'\s*(?!#)|(?!["'\s#]))""",
    re.IGNORECASE,
)


def sniff_logo(content: bytes) -> Optional[str]:
    """Return the image MIME type detected from the bytes themselves (never
    the client's claim), or None when the file is not an accepted logo.

    SVG is accepted only without scripts, event handlers, foreign objects or
    javascript: URLs; it is additionally served with a sandboxing CSP."""
    if not content:
        return None
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) > 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    head = content[:2048].lstrip()
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    if (head.startswith(b"<?xml") or head.startswith(b"<svg") or head.startswith(b"<!--")) \
            and b"<svg" in content[:4096]:
        if _SVG_FORBIDDEN.search(content):
            return None
        return "image/svg+xml"
    return None


@dataclass(frozen=True)
class Logo:
    data: bytes
    content_type: str
    filename: str = ""
    updated_at: str = ""

    @property
    def data_uri(self) -> str:
        return f"data:{self.content_type};base64,{base64.b64encode(self.data).decode()}"


def decode_logo(raw) -> Optional[Logo]:
    if not raw:
        return None
    if isinstance(raw, str):
        b64, ctype, fname, updated = raw, "", "", ""
    elif isinstance(raw, dict):
        b64 = raw.get("data") or raw.get("logo") or ""
        ctype = raw.get("content_type") or ""
        fname = raw.get("filename") or ""
        updated = raw.get("updated_at") or ""
    else:
        return None
    if not b64:
        return None
    if "," in b64 and b64.lstrip().startswith("data:"):
        b64 = b64.split(",", 1)[1]
    try:
        data = base64.b64decode(b64)
    except Exception:
        return None
    sniffed = sniff_logo(data)
    if not sniffed:
        return None
    return Logo(data=data, content_type=sniffed or ctype, filename=fname, updated_at=updated)


# ─── Branding model ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Branding:
    company_name: str = ""
    company_address: str = ""
    company_email: str = ""
    company_phone: str = ""
    company_website: str = ""
    base_url: str = ""
    timezone: str = "UTC"
    date_format: str = "YYYY-MM-DD"
    time_format: str = "24h"
    week_start: str = "monday"
    brand_color: str = DEFAULT_BRAND_COLOR
    report_footer: str = ""
    report_disclaimer: str = ""
    confidentiality: str = ""
    paper_size: str = "A4"
    logo: Optional[Logo] = field(default=None, compare=False)
    logo_dark: Optional[Logo] = field(default=None, compare=False)

    # --- identity -----------------------------------------------------------

    @property
    def display_name(self) -> str:
        """The organisation name, or the product name when none is set."""
        return self.company_name.strip() or PRODUCT_NAME

    @property
    def brand_rgb(self) -> tuple[int, int, int]:
        return hex_to_rgb(self.brand_color)

    @property
    def contact_line(self) -> str:
        return " · ".join(p for p in (self.company_website.strip(), self.company_email.strip(),
                                      self.company_phone.strip()) if p)

    def cover_logo(self) -> Optional[Logo]:
        """Logo for a dark background (falls back to the light logo)."""
        return self.logo_dark or self.logo

    # --- time ---------------------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    def localize(self, dt: datetime) -> datetime:
        """Convert to the organisation timezone; naive datetimes are UTC."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_utc_tz.utc)
        return dt.astimezone(self.tz)

    def tz_label(self, dt: Optional[datetime] = None) -> str:
        """Unambiguous zone label at ``dt``: 'UTC', or the offset such as
        'GMT+3', 'GMT-4', 'GMT+5:30' (abbreviations like IST/CST are ambiguous).
        The dashboard renders the same label (lib/branding.ts zoneLabel)."""
        if self.timezone in ("UTC", "Etc/UTC"):
            return "UTC"
        local = self.localize(dt or datetime.now(_utc_tz.utc))
        off = local.utcoffset()
        minutes = int(off.total_seconds() // 60) if off is not None else 0
        if minutes == 0:
            return "GMT"
        sign = "+" if minutes > 0 else "-"
        h, m = divmod(abs(minutes), 60)
        return f"GMT{sign}{h}" + (f":{m:02d}" if m else "")

    def fmt_date(self, dt: datetime) -> str:
        d = self.localize(dt)
        fmt = self.date_format
        if fmt == "DD/MM/YYYY":
            return f"{d.day:02d}/{d.month:02d}/{d.year}"
        if fmt == "MM/DD/YYYY":
            return f"{d.month:02d}/{d.day:02d}/{d.year}"
        if fmt == "DD.MM.YYYY":
            return f"{d.day:02d}.{d.month:02d}.{d.year}"
        if fmt == "DD-MMM-YYYY":
            return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"
        if fmt == "MMM D, YYYY":
            return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"
        return f"{d.year}-{d.month:02d}-{d.day:02d}"

    def fmt_time(self, dt: datetime, seconds: bool = False) -> str:
        d = self.localize(dt)
        if self.time_format == "12h":
            h = d.hour % 12 or 12
            suffix = "AM" if d.hour < 12 else "PM"
            return f"{h}:{d.minute:02d}" + (f":{d.second:02d}" if seconds else "") + f" {suffix}"
        return f"{d.hour:02d}:{d.minute:02d}" + (f":{d.second:02d}" if seconds else "")

    def fmt_datetime(self, dt: datetime, seconds: bool = False, with_tz: bool = False) -> str:
        out = f"{self.fmt_date(dt)} {self.fmt_time(dt, seconds)}"
        return f"{out} {self.tz_label(dt)}" if with_tz else out

    def fmt_short(self, dt: datetime) -> str:
        """Compact month-day + time for dense tables (e.g. 'Oct 07 14:05')."""
        d = self.localize(dt)
        return f"{_MONTHS[d.month - 1]} {d.day:02d} {self.fmt_time(dt)}"

    def period_label(self, frm: datetime, to: datetime) -> str:
        return f"{self.fmt_datetime(frm)} — {self.fmt_datetime(to)} {self.tz_label(to)}"

    # --- serialisation ------------------------------------------------------

    def public_dict(self) -> dict:
        """What any signed-in user (report viewers included) may read."""
        return {
            "company_name": self.company_name,
            "display_name": self.display_name,
            "company_address": self.company_address,
            "company_email": self.company_email,
            "company_phone": self.company_phone,
            "company_website": self.company_website,
            "timezone": self.timezone,
            "date_format": self.date_format,
            "time_format": self.time_format,
            "week_start": self.week_start,
            "brand_color": self.brand_color,
            "report_footer": self.report_footer,
            "report_disclaimer": self.report_disclaimer,
            "confidentiality": self.confidentiality,
            "paper_size": self.paper_size,
            "has_logo": self.logo is not None,
            "has_logo_dark": self.logo_dark is not None,
            "logo_version": (self.logo.updated_at if self.logo else "") or "",
            "logo_dark_version": (self.logo_dark.updated_at if self.logo_dark else "") or "",
        }


def branding_from_values(raw: Optional[dict], logo=None, logo_dark=None) -> Branding:
    """Sanitise stored company values into a Branding (never raises)."""
    v = raw if isinstance(raw, dict) else {}

    def s(key: str, limit: int, default: str = "") -> str:
        val = v.get(key, default)
        return (val if isinstance(val, str) else default).strip()[:limit]

    tz = s("timezone", 64, "UTC")
    date_fmt = s("date_format", 16, "YYYY-MM-DD")
    time_fmt = s("time_format", 4, "24h")
    week = s("week_start", 10, "monday").lower()
    color = s("brand_color", 7, DEFAULT_BRAND_COLOR)
    base_url = s("base_url", 255).rstrip("/")
    conf = s("confidentiality", 16)
    paper = s("paper_size", 8, "A4")
    return Branding(
        company_name=s("company_name", 120),
        company_address=s("company_address", 500),
        company_email=s("company_email", 254),
        company_phone=s("company_phone", 64),
        company_website=s("company_website", 255),
        base_url=base_url if valid_base_url(base_url) else "",
        timezone=tz if valid_timezone(tz) else "UTC",
        date_format=date_fmt if date_fmt in DATE_FORMATS else "YYYY-MM-DD",
        time_format=time_fmt if time_fmt in TIME_FORMATS else "24h",
        week_start=week if week in WEEK_STARTS else "monday",
        brand_color=color.upper() if valid_hex_color(color) else DEFAULT_BRAND_COLOR,
        report_footer=s("report_footer", 200),
        report_disclaimer=s("report_disclaimer", 1000),
        confidentiality=conf if conf in CONFIDENTIALITY_LEVELS else "",
        paper_size=paper if paper in PAPER_SIZES else "A4",
        logo=decode_logo(logo),
        logo_dark=decode_logo(logo_dark),
    )


async def load_branding(db: AsyncSession, with_logos: bool = True) -> Branding:
    keys = ["company"] + (list(LOGO_KEYS.values()) if with_logos else [])
    rows = (await db.execute(
        text("SELECT key, value FROM system_settings WHERE key = ANY(:keys)"),
        {"keys": keys},
    )).fetchall()
    values = {r[0]: r[1] for r in rows}
    return branding_from_values(
        values.get("company"),
        values.get(LOGO_KEYS["light"]),
        values.get(LOGO_KEYS["dark"]),
    )


def utc_now_iso() -> str:
    return datetime.now(_utc_tz.utc).isoformat()
