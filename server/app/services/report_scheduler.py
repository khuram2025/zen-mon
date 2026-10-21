"""Background scheduler for recurring report delivery.

A 60-second loop (started from main.py) fires every due, enabled
``report_schedules`` row. Each run:

* resolves its window with ``report_periods.resolve_period`` in the
  schedule's own timezone (``report_schedules.timezone``, migrate-163; the
  organisation timezone when unset) and the organisation week start, so
  "Previous month" is the local calendar month, as in the report viewer, and
  the period, generated time and email footer all name the zone the schedule
  runs in;
* renders built-in templates (``report_sections.REPORT_PRESETS``) and
  organisation templates (``custom_reports``) with the section engine the way
  the viewer does, as the schedule's owner; legacy report types keep their
  executive-summary pipeline;
* stores a token-gated ``report_runs`` artifact (the "View full report" share
  target) and attaches the report as PDF, Excel or HTML (CSV for legacy types);
* emails a branded summary to every linked email channel and to the
  schedule's direct recipients; other channel types get a KPI line and the
  link.

A schedule's hour and minute are wall-clock time in its own IANA timezone,
stored with the schedule: changing the organisation timezone later does not
move it. Next-run is that wall time converted to an absolute UTC instant
(``compute_next_run``, DST-aware). Manual runs ("Run now") are labelled as
such in the subject and body so they are never mistaken for scheduled ones.
Mirrors the discovery scheduler's shape (compute_next_run + tick loop) and
reuses the SMTP gateway resolution.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email import encoders
from email.mime.application import MIMEApplication
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import decrypt_config
from app.services import report_html, report_periods
from app.services.alert_schedule import get_configured_timezone, normalize_timezone
from app.services.branding import Branding, load_branding
from app.services.export_service import generate_excel_report, generate_csv_report
from app.services.report_data_service import build_executive
from app.services.report_service import generate_report

logger = logging.getLogger("zenplus.report_scheduler")

TICK_INTERVAL_S = 60

# Report types rendered by the legacy executive-summary pipeline. Every other
# type is a section-engine template: a built-in key or 'custom'.
LEGACY_REPORT_TYPES = ("executive_summary", "device_health", "service_health", "alert_analysis", "full_report")
LEGACY_FORMATS = ("pdf", "excel", "csv", "none")
# Legacy types whose sections read fleet-wide data (reports._LEGACY_UNSCOPABLE).
LEGACY_UNSCOPABLE = frozenset({"service_health", "full_report"})
TEMPLATE_FORMATS = ("pdf", "excel", "html", "csv", "none")
MAX_RECIPIENTS = 50

# Attachment format -> (file extension, MIME type).
ATTACHMENT_TYPES = {
    "pdf": ("pdf", "application/pdf"),
    "excel": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "html": ("html", "text/html"),
    "csv": ("csv", "text/csv"),
}
FORMAT_NAMES = {"pdf": "PDF", "excel": "Excel", "html": "HTML", "csv": "CSV"}

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}"
    r"@(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def is_email(value: Any) -> bool:
    """A plain addr-spec (no display name, comments or header separators)."""
    return isinstance(value, str) and len(value) <= 254 and bool(_EMAIL_RE.match(value)) \
        and ".." not in value.split("@", 1)[0] and not value.startswith(".") \
        and not value.split("@", 1)[0].endswith(".")


def normalize_recipients(values: Iterable[Any] | None) -> list[str]:
    """Lowercased, de-duplicated (order kept) email addresses. Raises
    ValueError naming the first invalid address, or when more than
    MAX_RECIPIENTS remain."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or ():
        addr = str(raw if raw is not None else "").strip().lower()
        if not addr:
            continue
        if not is_email(addr):
            raise ValueError(f"'{str(raw)[:80]}' is not a valid email address")
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    if len(out) > MAX_RECIPIENTS:
        raise ValueError(f"At most {MAX_RECIPIENTS} recipients per schedule")
    return out


# ---------------------------------------------------------------------------
# Next-run calculation
# ---------------------------------------------------------------------------

def _tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def schedule_timezone(sched: dict, fallback: str = "UTC") -> str:
    """The IANA zone a schedule's hour and minute are expressed in: its own
    stored ``timezone`` when that is a valid zone, else ``fallback`` (the
    organisation timezone), else UTC."""
    return normalize_timezone((sched or {}).get("timezone"), normalize_timezone(fallback))


def compute_next_run(sched: dict, tz_name: str, *, after: datetime | None = None) -> datetime | None:
    """Return the next fire time (UTC, tz-aware) for a schedule, or None.

    ``sched`` keys: frequency, hour, minute, day_of_week (1=Mon..7=Sun),
    day_of_month (1..31) and timezone. The wall-clock time is read in the
    schedule's own ``timezone``; ``tz_name`` (the organisation timezone) is
    only the fallback for a schedule without a valid one. "08:00 daily" in
    Asia/Riyadh is 05:00 UTC; in a DST zone the UTC instant follows the
    clock change while the local time stays 08:00.
    """
    if not sched.get("enabled", True):
        return None
    tz = _tz(schedule_timezone(sched, tz_name))
    now_local = (after or datetime.now(timezone.utc)).astimezone(tz)
    hour = int(sched.get("hour", 8) or 0)
    minute = int(sched.get("minute", 0) or 0)
    freq = sched.get("frequency", "daily")

    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if freq == "daily":
        if candidate <= now_local:
            candidate += timedelta(days=1)

    elif freq == "weekly":
        target_dow = int(sched.get("day_of_week") or 1)  # 1=Mon..7=Sun
        # Python weekday(): Mon=0..Sun=6  → ISO 1..7
        for add in range(0, 8):
            c = candidate + timedelta(days=add)
            if (c.weekday() + 1) == target_dow and c > now_local:
                candidate = c
                break
        else:
            candidate = candidate + timedelta(days=7)

    elif freq == "monthly":
        target_dom = int(sched.get("day_of_month") or 1)
        c = candidate.replace(day=min(target_dom, _days_in_month(candidate.year, candidate.month)))
        if c <= now_local:
            # advance one month
            year = c.year + (1 if c.month == 12 else 0)
            month = 1 if c.month == 12 else c.month + 1
            c = c.replace(year=year, month=month,
                          day=min(target_dom, _days_in_month(year, month)))
        candidate = c
    else:
        if candidate <= now_local:
            candidate += timedelta(days=1)

    return candidate.astimezone(timezone.utc)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        nxt = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return (nxt - timedelta(days=1)).day


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def local_time_label(sched: dict, tz_name: str, *, now: datetime | None = None) -> str:
    """The schedule's time of day in its own zone with the UTC equivalent of
    its next run: "08:00 Asia/Riyadh (05:00 UTC)", "01:00 Asia/Riyadh
    (22:00 UTC, previous day)", "08:00 UTC". In a DST zone the UTC part is
    the one of the next run."""
    zone = schedule_timezone(sched, tz_name)
    hour = int(sched.get("hour", 8) or 0)
    minute = int(sched.get("minute", 0) or 0)
    local = f"{hour:02d}:{minute:02d}"
    if zone in ("UTC", "Etc/UTC"):
        return f"{local} UTC"
    nxt = compute_next_run({**sched, "enabled": True, "timezone": zone}, zone, after=now)
    if nxt is None:
        return f"{local} {zone}"
    utc = nxt.astimezone(timezone.utc)
    wall = nxt.astimezone(_tz(zone))
    day = (", previous day" if utc.date() < wall.date() else
           ", next day" if utc.date() > wall.date() else "")
    return f"{local} {zone} ({utc:%H:%M} UTC{day})"


def schedule_text(sched: dict, tz_name: str, *, now: datetime | None = None) -> str:
    """'Daily at 08:00 Asia/Riyadh (05:00 UTC)', 'Weekly on Sunday at …',
    'Monthly on day 1 at …' (the dashboard's frequencyText plus the zone)."""
    at = local_time_label(sched, tz_name, now=now)
    freq = sched.get("frequency") or "daily"
    if freq == "weekly":
        dow = int(sched.get("day_of_week") or 1)
        return f"Weekly on {_WEEKDAYS[min(7, max(1, dow)) - 1]} at {at}"
    if freq == "monthly":
        return f"Monthly on day {int(sched.get('day_of_month') or 1)} at {at}"
    return f"Daily at {at}"


_TIME_IN_NAME = re.compile(r"(?<![\d:.])([01]?\d|2[0-3])[:h]([0-5]\d)(?:\s*([AaPp])\.?[Mm]\b\.?)?(?![\d:])")


def name_time_mismatch(name: Any, hour: Any, minute: Any) -> Optional[str]:
    """The time of day written in a schedule's name ("Daily NOC 08:00",
    "08h30 brief", "7:00 PM digest") when it differs from the stored local
    time, else None. A human-entered title is not evidence of when a schedule
    runs; the UI warns when the two disagree."""
    if not isinstance(name, str):
        return None
    try:
        h, m = int(hour), int(minute)
    except (TypeError, ValueError):
        return None
    found = _name_time(name, h, m)
    return found[0] if found else None


def _name_time(name: str, h: int, m: int) -> Optional[tuple[str, int, int]]:
    """(text, hour, minute) of the first time in ``name`` that differs from
    h:m, else None."""
    for match in _TIME_IN_NAME.finditer(name):
        nh, nm = int(match.group(1)), int(match.group(2))
        ampm = (match.group(3) or "").lower()
        if ampm:
            if not 1 <= nh <= 12:
                continue
            nh = (nh % 12) + (12 if ampm == "p" else 0)
        if (nh, nm) != (h, m):
            return match.group(0).strip(), nh, nm
    return None


def suggested_time(name: Any, hour: Any, minute: Any) -> Optional[dict]:
    """{hour, minute} written in a schedule's name when it differs from the
    stored time: the one-click correction the schedules page offers for
    schedules whose hour was entered already converted to UTC."""
    if not isinstance(name, str):
        return None
    try:
        h, m = int(hour), int(minute)
    except (TypeError, ValueError):
        return None
    found = _name_time(name, h, m)
    return {"hour": found[1], "minute": found[2]} if found else None



# ---------------------------------------------------------------------------
# SMTP resolution + send (attachment and inline images)
# ---------------------------------------------------------------------------

async def _resolve_smtp_gateway(db: AsyncSession, gateway_id: Any = None) -> dict | None:
    """SMTP gateway config: ``gateway_id``'s, else the default SMTP gateway,
    else the legacy ``system_settings['smtp']``. None when nothing usable is
    configured or the gateway is disabled."""
    if gateway_id:
        gw = (await db.execute(
            text("SELECT config, enabled FROM notification_gateways WHERE id = :id"), {"id": gateway_id}
        )).first()
    else:
        gw = (await db.execute(
            text("SELECT config, enabled FROM notification_gateways "
                 "WHERE type='smtp' AND is_default=true LIMIT 1")
        )).first()
    # The `enabled` column is authoritative — the Gateways page toggles it and
    # never touches config.enabled, so a gateway disabled in the UI would
    # otherwise keep sending.
    if gw is not None and not gw.enabled:
        return None
    gw_config = decrypt_config(gw.config) if gw else None
    if not gw_config:
        row = (await db.execute(
            text("SELECT value FROM system_settings WHERE key='smtp'")
        )).first()
        gw_config = decrypt_config(row[0]) if row and isinstance(row[0], dict) else None
    if not gw_config or not gw_config.get("host") or not gw_config.get("enabled", True):
        return None
    return gw_config


async def _resolve_smtp(db: AsyncSession, channel_row) -> dict | None:
    """Return SMTP gateway config dict for an email channel, or None."""
    cfg = decrypt_config(channel_row.config)
    return await _resolve_smtp_gateway(db, channel_row.gateway_id or cfg.get("gateway_id"))


def _clean_header(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", value or "").strip()


def _mime_part(payload: bytes, mime: str) -> MIMEBase:
    """A base64 part for ``mime`` ('application/pdf', 'text/html', or a bare
    application subtype such as 'pdf' as older callers pass)."""
    maintype, _, subtype = (mime if "/" in mime else f"application/{mime}").partition("/")
    if maintype == "application":
        return MIMEApplication(payload, _subtype=subtype)
    part = MIMEBase(maintype, subtype)
    part.set_payload(payload)
    encoders.encode_base64(part)
    if maintype == "text":
        part.set_param("charset", "utf-8")
    return part


def _msgid_domain(gw: dict) -> str:
    domain = str(gw.get("from_email") or "").rpartition("@")[2].strip()
    return domain if re.fullmatch(r"[A-Za-z0-9.-]{1,253}", domain or "") else "zenplus.local"


def build_email_message(gw: dict, recipients: list[str], subject: str, text_body: str, html_body: str,
                        attachment: tuple[str, bytes, str] | None = None,
                        inline_images: Iterable[tuple[str, bytes, str]] | None = None, *,
                        cc: Iterable[str] | None = None, reply_to: str | None = None) -> MIMEMultipart:
    """multipart/mixed { alternative { text, related { html, inline images } }, attachment }.

    ``attachment`` = (filename, bytes, MIME type); ``inline_images`` =
    (content id, bytes, MIME type) referenced from the HTML as ``cid:<id>``.
    ``cc`` and ``reply_to`` only take plain addresses (``is_email``)."""
    outer = MIMEMultipart("mixed")
    outer["From"] = formataddr((_clean_header(str(gw.get("from_name") or "ZenPlus")),
                                str(gw.get("from_email") or "")), charset="utf-8")
    outer["To"] = ", ".join(recipients)
    cc_list = [c for c in (cc or ()) if is_email(c)]
    if cc_list:
        outer["Cc"] = ", ".join(cc_list)
    if reply_to and is_email(reply_to):
        outer["Reply-To"] = reply_to
    outer["Subject"] = _clean_header(subject)
    outer["Date"] = formatdate(usegmt=True)
    outer["Message-ID"] = make_msgid(domain=_msgid_domain(gw))

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body, "plain"))
    html_part = MIMEText(html_body, "html")
    images = [i for i in (inline_images or ()) if i and i[0] and i[1]]
    if images:
        related = MIMEMultipart("related")
        related.attach(html_part)
        for cid, data, mime in images:
            subtype = (mime or "image/png").partition("/")[2] or "png"
            img = MIMEImage(data, _subtype=subtype)
            cid = str(cid).strip("<> ")
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline",
                           filename=f"{re.sub(r'[^A-Za-z0-9_-]+', '-', cid.split('@')[0]) or 'logo'}."
                                    f"{'jpg' if subtype == 'jpeg' else subtype}")
            related.attach(img)
        alt.attach(related)
    else:
        alt.attach(html_part)
    outer.attach(alt)

    if attachment:
        fname, payload, mime = attachment
        part = _mime_part(payload, mime)
        part.add_header("Content-Disposition", "attachment", filename=fname)
        outer.attach(part)
    return outer


def _smtp_connect(gw: dict) -> smtplib.SMTP:
    enc = gw.get("encryption", "tls")
    if enc == "ssl":
        server = smtplib.SMTP_SSL(gw["host"], int(gw.get("port", 465)), timeout=20,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(gw["host"], int(gw.get("port", 587)), timeout=20)
    try:
        if enc != "ssl":
            from app.core.smtp_tls import starttls_if_needed
            starttls_if_needed(server, enc)
        if gw.get("username"):
            server.login(gw["username"], gw.get("password", ""))
    except Exception:
        server.close()
        raise
    return server


def _smtp_close(server: smtplib.SMTP) -> None:
    try:
        server.quit()
    except Exception:
        server.close()


def _smtp_error(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [f"{code} {msg.decode(errors='replace') if isinstance(msg, bytes) else msg}"
                 for code, msg in exc.recipients.values()]
        return ("refused: " + "; ".join(codes))[:300]
    if isinstance(exc, smtplib.SMTPResponseException):
        msg = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        return f"{exc.smtp_code} {msg}"[:300]
    return f"{type(exc).__name__}: {exc}"[:300]


def _send_email_with_attachment(gw: dict, recipients: list[str], subject: str,
                                text_body: str, html_body: str,
                                attachment: tuple[str, bytes, str] | None = None,
                                inline_images: Iterable[tuple[str, bytes, str]] | None = None, *,
                                cc: Iterable[str] | None = None, reply_to: str | None = None) -> None:
    """Send one multipart email to ``recipients`` (an email channel's list, or
    a group-send schedule's direct recipients with their CC list).
    attachment = (filename, bytes, MIME type or bare application subtype)."""
    cc_list = [c for c in (cc or ()) if is_email(c)]
    msg = build_email_message(gw, recipients, subject, text_body, html_body, attachment, inline_images,
                              cc=cc_list, reply_to=reply_to)
    server = _smtp_connect(gw)
    try:
        server.sendmail(gw.get("from_email", ""), list(recipients) + cc_list, msg.as_string())
    finally:
        _smtp_close(server)


def _send_individually(gw: dict, recipients: list[str], subject: str, text_body: str, html_body: str,
                       attachment: tuple[str, bytes, str] | None = None,
                       inline_images: Iterable[tuple[str, bytes, str]] | None = None, *,
                       reply_to: str | None = None,
                       ) -> tuple[list[str], list[tuple[str, str]]]:
    """Send one message per direct recipient over a single SMTP session, so
    recipients (often external) never see each other's addresses. Returns
    (delivered addresses, [(address, error)]). Connection or login failures
    raise."""
    if not recipients:
        return [], []
    msg = build_email_message(gw, recipients[:1], subject, text_body, html_body, attachment, inline_images,
                              reply_to=reply_to)
    domain = _msgid_domain(gw)
    sent: list[str] = []
    failed: list[tuple[str, str]] = []
    server = _smtp_connect(gw)
    try:
        for i, addr in enumerate(recipients):
            del msg["To"]
            msg["To"] = addr
            del msg["Message-ID"]
            msg["Message-ID"] = make_msgid(domain=domain)
            try:
                server.sendmail(gw.get("from_email", ""), [addr], msg.as_string())
                sent.append(addr)
            except smtplib.SMTPServerDisconnected as exc:
                failed.extend((a, _smtp_error(exc)) for a in recipients[i:])
                break
            except smtplib.SMTPException as exc:
                # Refused for this recipient only (SMTPException is an OSError,
                # so this must come before the session-level handler).
                failed.append((addr, _smtp_error(exc)))
            except OSError as exc:
                # The session is gone (timeout, reset): the addresses already
                # sent stay recorded, the rest failed.
                failed.extend((a, _smtp_error(exc)) for a in recipients[i:])
                break
    finally:
        _smtp_close(server)
    return sent, failed


# ---------------------------------------------------------------------------
# Generation + delivery
# ---------------------------------------------------------------------------

class ReportUnavailable(Exception):
    """The schedule cannot produce a report (its template is gone, or nothing
    in it is visible to the owner). The message is shown in the run status."""


class _AttachmentUnavailable(Exception):
    pass


def _base_url(branding: Branding | None) -> str:
    s = get_settings()
    base = (s.APP_BASE_URL or "").strip()
    if not base and branding is not None:
        base = (branding.base_url or "").strip()
    return base.rstrip("/")


MANUAL_SUBJECT_TAG = "[Manual run]"
MANUAL_RUN_NOTE = ("Manual run: generated on request, not at the schedule's time. "
                   "Scheduled deliveries are not affected.")


NO_DATA_SUBJECT_TAG = "[No data]"


def email_subject(branding: Branding | None, title: str, period_label: str, *, manual: bool = False,
                  template: str | None = None, empty: bool = False, now: datetime | None = None) -> str:
    """'[Confidential] Monthly SLA — Previous month (1 Sep 2026 – 30 Sep 2026)';
    a manual run carries '[Manual run]' and a report without data '[No data]'
    after the classification. A schedule's ``subject_template`` replaces the
    title-and-period part."""
    prefix = f"[{branding.confidentiality}] " if branding is not None and branding.confidentiality else ""
    if manual:
        prefix += f"{MANUAL_SUBJECT_TAG} "
    if empty:
        prefix += f"{NO_DATA_SUBJECT_TAG} "
    body = f"{title} — {period_label}"
    if template and template.strip():
        tz = branding.timezone if branding is not None else "UTC"
        body = render_subject_template(template.strip(), title=title, period_label=period_label,
                                       generated=now or datetime.now(timezone.utc), tz=tz)
    return _clean_header(f"{prefix}{body}")[:255]


def _utc_clock(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%H:%M UTC")


def _schedule_branding(org: Branding, schedule: dict) -> Branding:
    """The organisation branding in the schedule's own timezone: the period,
    generated time and footer of a schedule's report name the zone it runs in."""
    zone = schedule_timezone(schedule, org.timezone)
    return org if zone == org.timezone else dataclasses.replace(org, timezone=zone)


def _naive_utc(dt: datetime) -> datetime:
    """The section engine's window convention (same as the viewer's)."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _legacy_window(period: str, start: datetime, end: datetime) -> tuple[str, datetime | None, datetime | None]:
    """(period, from, to) for the legacy generators: rolling periods as
    before; calendar windows as an explicit range. Their custom range pushes
    a midnight end to 23:59:59 of that day, so an exclusive midnight end is
    passed as the instant before it."""
    if period in report_periods.ROLLING_HOURS:
        return period, None, None
    if (end.hour, end.minute, end.second, end.microsecond) == (0, 0, 0, 0):
        end = end - timedelta(microseconds=1)
    return "custom", start, end


async def _schedule_owner(db: AsyncSession, schedule: dict):
    """The schedule's owner (a User), ORPHANED_OWNER when the owner is gone or
    deactivated (fail closed), or None for an ad-hoc run without a schedule."""
    if not schedule.get("id"):
        return None
    from app.models.user import User as _User
    from app.services.netflow_report import ORPHANED_OWNER

    owner_id = (await db.execute(text(
        "SELECT created_by FROM report_schedules WHERE id = :id"), {"id": schedule["id"]})).scalar()
    owner = await db.get(_User, owner_id) if owner_id else None
    if owner is None or not getattr(owner, "is_active", True):
        return ORPHANED_OWNER
    return owner


def _is_user(owner: Any) -> bool:
    from app.services.netflow_report import ORPHANED_OWNER
    return owner is not None and owner is not ORPHANED_OWNER


async def _template_for(db: AsyncSession, report_type: str, custom_id: Any,
                        owner: Any) -> tuple[str, str, str, list]:
    """(title, description, category label, entries) for a built-in key or the
    schedule's organisation template; ReportUnavailable when it was deleted
    or is private to someone other than the owner."""
    from app.services import report_render
    from app.services import report_sections as _sections

    if report_type != "custom":
        preset = _sections.REPORT_PRESETS.get(report_type)
        if not preset:
            raise ReportUnavailable(f"The report template '{report_type}' is no longer available; "
                                    "nothing was generated")
        return (preset["title"], preset.get("description") or "", preset.get("category") or "",
                preset.get("entries") or preset["sections"])

    row = await report_render.load_template(db, custom_id) if custom_id else None
    if row is None:
        raise ReportUnavailable("The organisation report template this schedule delivers was deleted; "
                                "nothing was generated. Choose another template or delete the schedule.")
    if _is_user(owner):
        from app.core.permissions import SUPERUSER_PERMISSION
        perms = await _sections.requester_permissions(db, owner)
        admin = perms is None or SUPERUSER_PERMISSION in perms
        if not report_render.can_read_template(row, owner, admin):
            raise ReportUnavailable(f"The report template '{row.name}' is private to another user; "
                                    "nothing was generated")
    from app.services import report_templates as _rt
    category = _rt.CATEGORY_TITLES.get(row.category or "", "") or "Custom report"
    return row.name, row.description or "", category, report_render.template_entries(row)


async def _owner_scope(db: AsyncSession, owner: Any, entries: list,
                       filters: dict) -> tuple[list[dict], dict, str]:
    """Entries, filters and scope label as the owner may see them: like the
    viewer, an owner limited to a visibility scope only runs scope-safe
    sections, pinned to the devices the owner can see (fail closed)."""
    from app.core import scoping
    from app.services import report_render
    from app.services import report_sections as _sections

    entries = _sections.section_entries(entries)[:report_render.MAX_SECTIONS]
    if not _is_user(owner):
        return entries, filters, await _scope_label(db, filters)
    scoped, ids = await scoping.scoped_device_filter(db, owner, filters.get("device_ids"))
    if not scoped:
        return entries, filters, await _scope_label(db, filters)
    kept = [e for e in entries if e["id"] in _sections.SCOPE_SAFE_SECTIONS]
    if not kept:
        raise ReportUnavailable("The schedule's owner is limited to a visibility scope and this report has "
                                "only fleet-wide sections; nothing was generated")
    omitted = len(entries) - len(kept)
    scoped_filters = {k: v for k, v in filters.items() if k != "application_id"}
    scoped_filters["device_ids"] = ids
    label = "Limited to the owner's visibility scope" + (
        f" ({omitted} fleet-wide section(s) omitted)" if omitted else "")
    return kept, scoped_filters, label


async def _scope_label(db: AsyncSession, filters: dict) -> str:
    """The report's scope in words, naming up to five devices or the chosen
    groups/locations (``report_render.scope_label``)."""
    from app.services import report_render
    label_fn = getattr(report_render, "scope_label", None)
    if label_fn is None:
        return report_render.scope_label_for(filters)
    return await label_fn(db, filters)


def _email_logo(sections_mod: Any, meta: dict) -> Optional[tuple[str, bytes, str]]:
    """(content id, bytes, MIME) of the organisation logo for the email body,
    PNG/JPEG only (``report_sections.email_logo_attachment``)."""
    fn = getattr(sections_mod, "email_logo_attachment", None)
    if fn is None:
        return None
    try:
        logo = fn(meta)
    except Exception:
        logger.warning("report email logo unavailable", exc_info=True)
        return None
    if not logo:
        return None
    cid, data, mime = logo
    if not cid or not data or mime not in ("image/png", "image/jpeg"):
        return None
    return str(cid).strip("<> "), bytes(data), mime


def _not_generated(message: str) -> dict:
    return {"run_id": None, "token": None, "view_url": None,
            "delivered": [], "errors": [message], "status": "failed"}


def short_error(exc: BaseException) -> str:
    """One readable line for a run or schedule error. SMTP and network errors
    keep their message; database errors keep the driver's first line without
    SQL text, parameter dumps or documentation links; anything else is only
    its class name (an unexpected message may carry a DSN or a secret)."""
    if isinstance(exc, (smtplib.SMTPException, OSError)):
        return _smtp_error(exc)
    from sqlalchemy.exc import SQLAlchemyError
    if not isinstance(exc, SQLAlchemyError):
        return type(exc).__name__
    msg = str(getattr(exc, "orig", None) or exc).strip().splitlines()
    first = (msg[0] if msg else "").split("[SQL:")[0].strip()
    first = re.sub(r"^\(?[\w.]+\)?\s*", "", first) if first.startswith("(") else first
    first = re.sub(r"<class '[^']+'>:\s*", "", first)
    return f"{type(exc).__name__}: {first}"[:300] if first else type(exc).__name__


def _retryable(error: str) -> bool:
    """A delivery error worth retrying: connection and temporary (4xx) SMTP
    failures, not a permanent refusal (5xx) of the address."""
    m = re.search(r"\b([45])\d\d\b", error or "")
    return not (m and m.group(1) == "5")


def _http_failure(err: Any) -> tuple[str, bool]:
    """(message, retryable) for a webhook-family channel error: a server error
    (5xx), 429 or a network failure is retried; any other HTTP status (a
    wrong URL, a revoked token) is not."""
    response = getattr(err, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return f"HTTP {code}", code >= 500 or code == 429
    return (short_error(err) if isinstance(err, BaseException) else str(err)[:300]), True


def _section_failed(sections_mod: Any, s: dict) -> bool:
    fn = getattr(sections_mod, "section_failed", None)
    if fn is not None:
        return bool(fn(s))
    return bool(s.get("error"))


def _report_is_empty(sections_mod: Any, secs: list[dict]) -> bool:
    """No section of the report holds a figure, a table row or a chart (a
    report over a period without data, e.g. a month before monitoring
    started). A KPI of "—" is no figure; a 0 is one."""
    fn = getattr(sections_mod, "is_empty_report", None)
    if fn is not None:
        return bool(fn(secs))
    for s in secs:
        if s.get("restricted") or s.get("error"):
            continue
        if any((t or {}).get("rows") for t in s.get("tables") or []):
            return False
        if s.get("charts"):
            return False
        if any(str(k.get("value") if k.get("value") is not None else "").strip() not in ("", "—", "-")
               for k in s.get("kpis") or []):
            return False
    return True


_SUBJECT_PLACEHOLDER = re.compile(r"\{(title|period|date)\}")


def render_subject_template(template: str, *, title: str, period_label: str, generated: datetime,
                            tz: str) -> str:
    """A schedule's own subject: ``{title}``, ``{period}`` and ``{date}`` (the
    local generation date) are replaced; anything else, format specs
    included, is kept as written (never str.format: ``{title:>999999999}``
    would allocate gigabytes)."""
    values = {"title": title, "period": period_label,
              "date": generated.astimezone(_tz(tz)).strftime("%Y-%m-%d")}
    return _SUBJECT_PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


async def _resolve_scope(db: AsyncSession, filters: dict) -> dict:
    """Group, location and device-type scope of a template schedule resolved
    to device ids (``report_render.resolve_device_scope``). ReportUnavailable
    when the scope matches no device: an empty id list would mean the whole
    estate."""
    keys = ("group_ids", "locations", "device_types")
    if not any(filters.get(k) for k in keys):
        return filters
    from app.services import report_render
    resolver = getattr(report_render, "resolve_device_scope", None)
    if resolver is None:
        raise ReportUnavailable("Group and location scopes are not supported by this version; "
                                "nothing was generated")
    ids = await resolver(db, device_ids=filters.get("device_ids") or None,
                         group_ids=filters.get("group_ids") or None,
                         locations=filters.get("locations") or None,
                         device_types=filters.get("device_types") or None)
    if ids is None:
        return filters
    if not ids:
        raise ReportUnavailable("The schedule's group/location scope matches no device; nothing was generated")
    return {**filters, "device_ids": list(ids)}


async def _deliver(db: AsyncSession, schedule: dict, *, channel_ids: list, direct: list[str],
                   subject: str, email_text: str, email_html: str,
                   attachment: Optional[tuple[str, bytes, str]], inline_images: list,
                   channel_message: str, ctx: dict) -> tuple[list[str], list[str], list[dict], list[str]]:
    """Send to channels and direct recipients. Returns (delivered names or
    addresses, error lines, retryable failed targets, permanent failures —
    the error lines that will not be retried)."""
    delivered: list[str] = []
    errors: list[str] = []
    failed: list[dict] = []
    permanent: list[str] = []

    def fail(name: str, err: str, target: Optional[dict] = None, retryable: Optional[bool] = None) -> None:
        errors.append(f"{name}: {err}")
        if target is not None and (_retryable(err) if retryable is None else retryable):
            failed.append({**target, "error": err})
        else:
            permanent.append(f"{name}: {err}")

    reply_to = schedule.get("reply_to") or None
    for cid in channel_ids:
        name = str(cid)
        try:
            ch = (await db.execute(
                text("SELECT id, name, type, config, enabled, gateway_id "
                     "FROM notification_channels WHERE id = CAST(:id AS uuid)"), {"id": str(cid)}
            )).first()
            if not ch or not ch.enabled:
                fail(str(cid), "channel missing/disabled")
                continue
            name = ch.name
            cfg = decrypt_config(ch.config)

            if ch.type == "email":
                recipients = [r.strip() for r in cfg.get("recipients", "").split(",") if r.strip()]
                if not recipients:
                    fail(ch.name, "no recipients")
                    continue
                gw = await _resolve_smtp(db, ch)
                if not gw:
                    fail(ch.name, "no SMTP gateway")
                    continue
                await asyncio.to_thread(_send_email_with_attachment, gw, recipients, subject, email_text,
                                        email_html, attachment, inline_images, reply_to=reply_to)
                delivered.append(ch.name)
                continue

            if ch.type == "sms":
                from app.api.v1.alert_engine import _send_sms
                from app.services.host_alert_service import _gateway_config
                phones = cfg.get("phone_numbers", "")
                gw = await _gateway_config(db, ch, "sms")
                if phones and gw:
                    await _send_sms(gw, phones, channel_message)
                    delivered.append(ch.name)
                else:
                    fail(ch.name, "no phone numbers or SMS gateway")
            else:
                from app.api.v1.alert_engine import _dispatch_channel_result
                ok, err = await _dispatch_channel_result(ch.type, cfg, ctx)
                if ok:
                    delivered.append(ch.name)
                elif err is None:
                    fail(ch.name, f"unsupported or unconfigured channel type '{ch.type}'")
                else:
                    msg, again = _http_failure(err)
                    fail(ch.name, msg, {"kind": "channel", "id": str(cid), "name": ch.name}, retryable=again)
        except Exception as exc:
            logger.warning("report delivery to channel %s failed", cid, exc_info=True)
            fail(name, short_error(exc), {"kind": "channel", "id": str(cid), "name": name})

    if direct:
        try:
            gw = await _resolve_smtp_gateway(db)
            if not gw:
                fail(f"{len(direct)} direct recipient(s)", "no SMTP gateway is configured")
            elif schedule.get("group_send"):
                cc = [c for c in (schedule.get("cc") or []) if is_email(c) and c not in direct]
                await asyncio.to_thread(_send_email_with_attachment, gw, direct, subject, email_text,
                                        email_html, attachment, inline_images, cc=cc, reply_to=reply_to)
                delivered.extend(direct + cc)
            else:
                sent, bad = await asyncio.to_thread(_send_individually, gw, direct, subject, email_text,
                                                    email_html, attachment, inline_images, reply_to=reply_to)
                delivered.extend(sent)
                for addr, err in bad:
                    fail(addr, err, {"kind": "email", "id": addr, "name": addr})
        except Exception as exc:
            logger.warning("report delivery to direct recipients failed (schedule %s)", schedule.get("id"),
                           exc_info=True)
            err = short_error(exc)
            errors.append(f"{len(direct)} direct recipient(s): {err}")
            targets = direct + ([c for c in (schedule.get("cc") or []) if is_email(c)]
                                if schedule.get("group_send") else [])
            if _retryable(err):
                failed.extend({"kind": "email", "id": a, "name": a, "error": err} for a in targets)
            else:
                permanent.append(f"{len(direct)} direct recipient(s): {err}")
    return delivered, errors, failed, permanent


def _channel_message(title: str, plabel: str, kpi_pairs: list[tuple[str, str]], view_url: Optional[str],
                     manual: bool) -> str:
    body = f"{MANUAL_SUBJECT_TAG} " if manual else ""
    body += f"{title} — {plabel}"
    if kpi_pairs:
        body += "\n" + " · ".join(f"{l}: {v}" for l, v in kpi_pairs[:4])
    if view_url:
        body += f"\n{view_url}"
    return body


def _channel_ctx(subject: str, message: str, branding_name: str, kpi_pairs: list, view_url: Optional[str],
                 schedule_id: Any, title: str) -> dict:
    return {
        "subject": subject, "message": message, "body": message,
        "hostname": branding_name, "ip_address": "",
        "status": "REPORT", "severity": "info",
        "details": list(kpi_pairs) + ([("View report", view_url)] if view_url else []),
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "rule_id": str(schedule_id or ""), "rule_name": title,
    }


def _final_status(*, targets: bool, delivered: list, errors: list, notes: list) -> str:
    if not targets or (delivered and not errors):
        status = "success"
    elif delivered:
        status = "partial"
    else:
        status = "failed"
    if notes and status == "success":
        status = "partial"
    return status


NO_DATA_NOTE = "The report has no data for this period."
NOT_DELIVERED_PREFIX = "Not delivered (not retried): "
NO_LINK_NOTE = ("This email has no link to the full report because no base URL is configured: "
                "open ZenPlus → Reports → Delivered reports to view it.")


async def generate_and_deliver(db: AsyncSession, schedule: dict, *, triggered_by: str = "scheduled") -> dict:
    """Render a report for ``schedule`` and deliver it to its channels and
    direct recipients.

    Returns a summary dict {run_id, token, view_url, delivered, errors,
    status}. The report_runs row is written first as ``running`` and
    finished with the outcome, the email as sent (for retries) and the
    attachment (for the run history); retryable failures get a
    ``next_retry_at``. A schedule whose template is gone records nothing and
    fails with a clear error."""
    from app.services import report_settings

    report_type = schedule["report_type"]
    period = schedule.get("period") or "last_24h"
    fmt = schedule.get("format") or "pdf"
    filters = {k: v for k, v in (schedule.get("filters") or {}).items() if k != "netflow_withheld"}

    manual = triggered_by == "manual"
    settings = await report_settings.load(db)
    branding = _schedule_branding(await load_branding(db), schedule)
    start, end, plabel = report_periods.resolve_period(period, branding.timezone, branding.week_start)

    from app.services import report_render
    from app.services import report_sections as _sections
    is_sections = report_type == "custom" or report_type in _sections.REPORT_PRESETS
    if not is_sections and report_type not in LEGACY_REPORT_TYPES:
        return _not_generated(f"The report template '{report_type}' is no longer available; nothing was generated")

    data = None
    secs: list[dict] = []
    sec_meta: dict = {}
    # Problems that do not stop delivery but make the run less than a clean success.
    run_notes: list[str] = []
    # Data follows the schedule owner's permissions and visibility. A schedule
    # whose owner is gone or deactivated withholds them (it would otherwise run
    # with the appliance's unrestricted view).
    owner = await _schedule_owner(db, schedule)
    # Legacy attachments list alert messages: a NetFlow alert's is withheld
    # unless the owner has netflow.view (X-01). Everything about the owner is
    # decided before the run row is committed (a commit may expire the owner's
    # loaded attributes).
    netflow_withheld = False
    legacy_tags: Optional[list[str]] = None
    if not is_sections:
        from app.api.v1.netflow.access import VIEW_PERMISSION
        from app.core import scoping
        from app.services.netflow_report import flow_visibility_for

        from app.services.netflow_report import ORPHANED_OWNER
        if owner is ORPHANED_OWNER:
            return _not_generated("The schedule's owner is no longer active; nothing was generated. "
                                  "Assign the schedule to an active user.")
        netflow_withheld = not (await flow_visibility_for(db, owner)).can(VIEW_PERMISSION)
        if _is_user(owner):
            tags = await scoping.visible_tags(db, owner)
            legacy_tags = list(tags) if tags is not None else None
            # The attachment too is confined to what the owner may see (as
            # POST /reports/generate does); fleet-wide types are refused.
            scoped, ids = await scoping.scoped_device_filter(db, owner, filters.get("device_ids"))
            if scoped:
                if report_type in LEGACY_UNSCOPABLE:
                    return _not_generated("The schedule's owner is limited to a visibility scope and this classic "
                                          "report has fleet-wide sections; nothing was generated. Convert the "
                                          "schedule to its template.")
                filters = {k: v for k, v in filters.items()
                           if k not in ("group_ids", "locations", "device_types")}
                filters["device_ids"] = ids
    if is_sections:
        try:
            default_title, description, category, entries = await _template_for(
                db, report_type, schedule.get("custom_report_id"), owner)
            filters = await _resolve_scope(db, filters)
            entries, filters, scope_label = await _owner_scope(db, owner, entries, filters)
        except ReportUnavailable as exc:
            logger.warning("report schedule %s not generated: %s", schedule.get("id"), exc)
            return _not_generated(str(exc))
        title = schedule.get("name") or default_title
        if filters.get("netflow") is not None:
            # A stored scope the API would now reject (saved before
            # validation existed): the run cannot be a clean success.
            from app.api.v1.netflow.common import NetflowError
            from app.services import netflow_classify, netflow_groups, netflow_report as _nfr

            await netflow_classify.load_rules()
            await netflow_groups.load_groups()
            try:
                _nfr.scope_from_mapping(filters["netflow"])
            except NetflowError as exc:
                run_notes.append(f"NetFlow scope is not valid ({exc.detail}); NetFlow sections were not generated")
        frm, to = _naive_utc(start), _naive_utc(end)
        secs = await _sections.build_sections(db, entries, frm, to, filters or None,
                                              requester=owner, branding=branding)
        sec_meta = await _sections.build_report_meta(
            db, title, frm, to, description=description, category=category,
            scope_label=scope_label, branding=branding)
        failed_titles = [s.get("title") or s.get("id") for s in secs if _section_failed(_sections, s)]
        if failed_titles:
            run_notes.append("Section(s) could not be generated: " + ", ".join(map(str, failed_titles[:8])))
    else:
        # Legacy types: executive dataset drives the universal HTML/email summary,
        # limited to the owner's visibility scope.
        data = await build_executive(db, start, end, visible_tags=legacy_tags)
        title = schedule.get("name") or report_html.report_title(report_type)

    empty = is_sections and _report_is_empty(_sections, secs)
    generated_at = datetime.now(timezone.utc)
    meta = {
        "title": title,
        "report_type": report_type,
        "period": period,
        "period_label": plabel,
        "generated_at": generated_at,
        "company_name": branding.display_name,
        "product_name": "ZenPlus",
    }
    # Every label names the schedule's zone and, outside UTC, the UTC clock:
    # recipients compare the generated time with the receipt time.
    run_facts: dict[str, Any] = {"run_kind": "manual" if manual else "scheduled",
                                 "schedule_name": schedule.get("name") or ""}
    if schedule.get("id") and schedule.get("hour") is not None:
        run_facts["schedule_label"] = schedule_text(schedule, branding.timezone, now=generated_at)
    if manual:
        run_facts["run_note"] = MANUAL_RUN_NOTE
    if not is_sections:
        # Legacy reports carry the same branding as template reports: logo,
        # colour, footer, disclaimer, window and generated time in the
        # schedule's timezone (report_html reads these keys).
        legacy_meta = await _sections.build_report_meta(
            db, title, _naive_utc(start), _naive_utc(end), branding=branding)
        meta = {**legacy_meta, **meta, "start": start, "end": end,
                "generated_label": legacy_meta["generated_label"]}
    for m in ((sec_meta,) if is_sections else (meta,)):
        if m.get("generated_label") and branding.timezone not in ("UTC", "Etc/UTC"):
            m["generated_label"] = f"{m['generated_label']} ({_utc_clock(m.get('generated_at') or generated_at)})"
        m.update(run_facts)

    # Persist a shareable HTML artifact.
    token = secrets.token_urlsafe(24)
    # Rendering is CPU-bound (matplotlib charts, fpdf2, openpyxl): keep it off
    # the event loop, which also serves API requests on this worker.
    if is_sections:
        html_page = await asyncio.to_thread(_sections.render_html, sec_meta, secs)
    else:
        html_page = await asyncio.to_thread(report_html.build_report_html, meta, data)
    expiry_days = settings["link_expiry_days"]
    run_row = (await db.execute(
        text("""INSERT INTO report_runs
                (schedule_id, report_type, period, title, token, html, status, generated_at,
                 expires_at, kind, window_from, window_to)
                VALUES (:sid, :rt, :pd, :title, :token, :html, 'running', NOW(),
                        CASE WHEN CAST(:exp AS int) > 0 THEN NOW() + make_interval(days => CAST(:exp AS int)) END,
                        :kind, :wf, :wt)
                RETURNING id"""),
        {"sid": schedule.get("id"), "rt": report_type, "pd": period,
         "title": (f"{meta['title']} (manual run)" if manual else meta["title"])[:255],
         "token": token, "html": html_page, "exp": expiry_days,
         "kind": "manual" if manual else "scheduled", "wf": start, "wt": end},
    )).first()
    run_id = str(run_row.id)
    await db.commit()

    try:
        return await _finish_run(
            db, schedule, run_id=run_id, token=token, manual=manual, settings=settings,
            branding=branding, is_sections=is_sections, secs=secs, sec_meta=sec_meta, meta=meta,
            data=data, html_page=html_page, fmt=fmt, period=period, start=start, end=end,
            plabel=plabel, filters=filters, title=title, report_type=report_type,
            netflow_withheld=netflow_withheld, run_notes=run_notes, empty=empty)
    except Exception as exc:
        # Never leave the run "running": record the failure, then let the
        # caller record it on the schedule.
        logger.exception("report run %s failed (schedule %s)", run_id, schedule.get("id"))
        await db.rollback()
        err = short_error(exc)
        await db.execute(text("UPDATE report_runs SET status = 'failed', error = :e WHERE id = :id"),
                         {"e": f"The run failed: {err}", "id": run_id})
        await db.commit()
        return {"run_id": run_id, "token": token, "view_url": None, "delivered": [],
                "errors": [f"The run failed: {err}"], "status": "failed"}


async def _finish_run(db: AsyncSession, schedule: dict, *, run_id: str, token: str, manual: bool,
                      settings: dict, branding: Branding, is_sections: bool, secs: list, sec_meta: dict,
                      meta: dict, data: Any, html_page: str, fmt: str, period: str, start: datetime,
                      end: datetime, plabel: str, filters: dict, title: str, report_type: str,
                      netflow_withheld: bool, run_notes: list[str], empty: bool) -> dict:
    from app.services import report_render
    from app.services import report_sections as _sections

    base = _base_url(branding)
    view_url = f"{base}/api/v1/reports/shared/{token}" if base else None

    if empty and schedule.get("skip_when_empty"):
        note = "The report has no data for this period; it was not sent (the schedule skips empty reports)."
        await db.execute(
            text("UPDATE report_runs SET status = 'skipped', error = NULL, notes = CAST(:n AS jsonb) "
                 "WHERE id = :id"),
            {"n": json.dumps([note]), "id": run_id})
        await db.commit()
        return {"run_id": run_id, "token": token, "view_url": view_url, "delivered": [],
                "errors": [], "notes": [note], "status": "skipped"}
    info_notes = [NO_DATA_NOTE] if empty else []

    # Build the formal attachment.
    attachment = None
    delivery_notes: list[str] = []
    if fmt != "none":
        try:
            ext, mime = ATTACHMENT_TYPES.get(fmt) or ("", "")
            if not ext:
                raise _AttachmentUnavailable(f"Unknown attachment format '{fmt}'")
            if fmt == "html":
                payload = html_page.encode("utf-8")
            elif is_sections:
                if fmt == "pdf":
                    payload = bytes(await asyncio.to_thread(_sections.render_pdf, sec_meta, secs))
                elif fmt == "excel":
                    from app.services import report_xlsx
                    payload = bytes(await asyncio.to_thread(report_xlsx.render_xlsx, sec_meta, secs))
                elif fmt == "csv":
                    try:
                        from app.services import report_csv
                    except ImportError:
                        raise _AttachmentUnavailable("CSV attachments are not available in this version") from None
                    payload = bytes(await asyncio.to_thread(report_csv.render_csv, sec_meta, secs))
                else:
                    raise _AttachmentUnavailable(f"{FORMAT_NAMES.get(fmt, fmt)} attachments are not available "
                                                 "for template reports; choose PDF, Excel, CSV or HTML")
            else:
                legacy_period, legacy_from, legacy_to = _legacy_window(period, start, end)
                common = dict(db=db, report_type=report_type, period=legacy_period,
                              from_time=legacy_from, to_time=legacy_to,
                              device_ids=filters.get("device_ids"),
                              group_ids=filters.get("group_ids"),
                              locations=filters.get("locations"),
                              device_types=filters.get("device_types"),
                              redact_netflow=netflow_withheld)
                if fmt == "excel":
                    payload = await generate_excel_report(**common)
                elif fmt == "csv":
                    payload = await generate_csv_report(**common)
                    if isinstance(payload, str):
                        payload = payload.encode("utf-8")
                else:
                    payload = await generate_report(**common)
            attachment = (report_render.report_filename(title, ext, branding), bytes(payload), mime)
        except _AttachmentUnavailable as exc:
            run_notes.append(str(exc))
        except Exception as exc:
            logger.exception("report attachment generation failed (schedule %s)", schedule.get("id"))
            run_notes.append(f"The {FORMAT_NAMES.get(fmt, fmt)} attachment could not be generated "
                             f"({type(exc).__name__}); the email was sent without it")
    limit_mb = settings["max_attachment_mb"]
    if attachment is not None and len(attachment[1]) > limit_mb * 1024 * 1024:
        size_mb = len(attachment[1]) / (1024 * 1024)
        note = (f"The {FORMAT_NAMES.get(fmt, fmt)} attachment ({size_mb:.1f} MB) is over the "
                f"{limit_mb} MB limit and was left out.")
        run_notes.append(note)
        delivery_notes.append(note)
        attachment = None
    if not view_url:
        delivery_notes.append(NO_LINK_NOTE)

    inline_images: list[tuple[str, bytes, str]] = []
    logo = _email_logo(_sections, sec_meta if is_sections else meta)
    if logo:
        inline_images.append(logo)
    if delivery_notes:
        (sec_meta if is_sections else meta)["delivery_note"] = " ".join(delivery_notes)
    if is_sections:
        extra: dict[str, Any] = {"logo_cid": logo[0]} if logo else {}
        email_html = await asyncio.to_thread(
            lambda: _sections.email_summary_html(sec_meta, secs, view_url,
                                                 attached=attachment is not None, **extra))
        email_text = _sections.email_summary_text(sec_meta, secs, view_url)
    else:
        email_meta = {**meta, "logo_cid": logo[0]} if logo else meta
        email_html = report_html.build_report_email_html(email_meta, data, view_url,
                                                         attached=attachment is not None)
        email_text = report_html.build_report_email_text(meta, data, view_url)
    subject = email_subject(branding, meta["title"], plabel, manual=manual,
                            template=schedule.get("subject_template"), empty=empty)

    # Compact KPI pairs for non-email channel payloads: the same per-section
    # groups as the email, each figure prefixed with its section.
    kpi_pairs: list[tuple[str, str]] = []
    if is_sections:
        for g in _sections.email_kpi_groups(secs, sec_meta):
            for k in g["kpis"]:
                kpi_pairs.append((f"{g['title']}: {k.get('label') or ''}", str(k.get("value") or "")))
    elif data:
        k = data.get("kpis") or {}
        if k.get("availability_pct") is not None:
            kpi_pairs.append(("Availability", f"{k['availability_pct']:.2f}%"))
        kpi_pairs.append(("Incidents", str(k.get("incidents_count") or 0)))
        kpi_pairs.append(("Devices monitored", str(k.get("devices_monitored") or 0)))
    message = _channel_message(meta["title"], plabel, kpi_pairs, view_url, manual)
    ctx = _channel_ctx(subject, message, branding.display_name, kpi_pairs, view_url,
                       schedule.get("id"), meta["title"])

    channel_ids = schedule.get("notify_channels") or []
    direct = [r for r in (str(x).strip().lower() for x in (schedule.get("recipients") or [])) if is_email(r)]
    direct = list(dict.fromkeys(direct))[:MAX_RECIPIENTS]
    delivered, errors, failed, permanent = await _deliver(
        db, schedule, channel_ids=channel_ids, direct=direct, subject=subject, email_text=email_text,
        email_html=email_html, attachment=attachment, inline_images=inline_images,
        channel_message=message, ctx=ctx)
    # Kept with the run: a later successful retry of the other targets must
    # not make these disappear from its history.
    undelivered = [f"{NOT_DELIVERED_PREFIX}{p}" for p in permanent]

    status = _final_status(targets=bool(channel_ids or direct), delivered=delivered, errors=errors,
                           notes=run_notes)
    next_retry = None
    if failed and settings["retry_attempts"] > 0:
        from app.services.report_settings import retry_delay_minutes
        next_retry = datetime.now(timezone.utc) + timedelta(minutes=retry_delay_minutes(1))
    all_errors = errors + [n for n in run_notes if n not in errors]
    await db.execute(
        text("""UPDATE report_runs
                SET delivered_to = CAST(:d AS jsonb), status = :st, error = :err,
                    notes = CAST(:notes AS jsonb), failed_targets = CAST(:ft AS jsonb),
                    next_retry_at = :nr, email_subject = :subj, email_html = :eh, email_text = :et,
                    artifact = :art, artifact_name = :an, artifact_mime = :am
                WHERE id = :id"""),
        {"d": json.dumps(delivered), "st": status,
         "err": "; ".join(errors) if errors else None,
         "notes": json.dumps(run_notes + undelivered + info_notes), "ft": json.dumps(failed), "nr": next_retry,
         "subj": subject, "eh": email_html, "et": email_text,
         "art": attachment[1] if attachment else None,
         "an": attachment[0] if attachment else None,
         "am": attachment[2] if attachment else None, "id": run_id},
    )
    await db.commit()

    return {"run_id": run_id, "token": token, "view_url": view_url,
            "delivered": delivered, "errors": all_errors, "notes": run_notes + info_notes,
            "status": status, "retry_at": next_retry.isoformat() if next_retry else None}


async def retry_run(db: AsyncSession, run_id: str, *, manual: bool = False) -> dict:
    """Send a run's stored email again to the targets it still owes
    (``failed_targets``). Counts as one attempt; schedules the next one with
    backoff until ``retry_attempts`` is used up."""
    from app.services import report_settings
    from app.services import report_sections as _sections

    row = (await db.execute(text(
        """SELECT r.id, r.schedule_id, r.title, r.token, r.status, r.delivered_to, r.failed_targets,
                  r.retry_count, r.email_subject, r.email_html, r.email_text, r.artifact,
                  r.artifact_name, r.artifact_mime, r.window_from, r.window_to, r.notes, r.error,
                  s.reply_to, s.group_send, s.cc, s.name AS schedule_name, s.timezone
           FROM report_runs r LEFT JOIN report_schedules s ON s.id = r.schedule_id
           WHERE r.id = CAST(:id AS uuid)"""), {"id": str(run_id)})).first()
    if row is None:
        return {"status": "missing", "delivered": [], "errors": ["Run not found"]}
    # One retry of a run at a time (the tick on either worker, or a manual
    # retry): held until this transaction's commit at the end.
    locked = (await db.execute(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"),
                               {"k": f"report_run_retry:{row.id}"})).scalar()
    if locked is False:
        return {"status": "busy", "delivered": [], "errors": ["A retry of this run is already in progress"],
                "remaining": len(row.failed_targets or [])}
    targets = list(row.failed_targets or [])
    if not targets:
        await db.execute(text("UPDATE report_runs SET next_retry_at = NULL WHERE id = :id"), {"id": row.id})
        await db.commit()
        return {"status": row.status, "delivered": [], "errors": [], "remaining": 0}
    if not row.email_html:
        await db.execute(text("UPDATE report_runs SET next_retry_at = NULL, failed_targets = '[]'::jsonb, "
                              "error = concat_ws('; ', error, 'Retry not possible: the stored email was "
                              "removed by the retention policy') WHERE id = :id"), {"id": row.id})
        await db.commit()
        return {"status": row.status, "delivered": [],
                "errors": ["The stored email was removed by the retention policy"], "remaining": 0}

    settings = await report_settings.load(db)
    attempt = int(row.retry_count or 0) + 1
    attachment = ((row.artifact_name, bytes(row.artifact), row.artifact_mime)
                  if row.artifact is not None and row.artifact_name else None)
    inline_images: list[tuple[str, bytes, str]] = []
    try:
        branding = await load_branding(db)
        frm = _naive_utc(row.window_from or datetime.now(timezone.utc))
        to = _naive_utc(row.window_to) if row.window_to else frm
        logo_meta = await _sections.build_report_meta(db, row.title, frm, to, branding=branding)
        logo = _email_logo(_sections, logo_meta)
        if logo:
            inline_images.append(logo)
    except Exception:
        logger.debug("logo for report retry unavailable", exc_info=True)
        branding = None
    sched = {"id": str(row.schedule_id) if row.schedule_id else None, "reply_to": row.reply_to,
             "group_send": bool(row.group_send), "cc": list(row.cc or [])}
    channel_ids = [t["id"] for t in targets if t.get("kind") == "channel"]
    direct = [t["id"] for t in targets if t.get("kind") == "email"]
    if sched["group_send"]:
        # A group message failed as a whole: resend it to the owed addresses,
        # keeping CC'd people on the CC line.
        owed = set(direct)
        cc_all = set(sched["cc"])
        direct = [a for a in direct if a not in cc_all]
        sched["cc"] = [a for a in sched["cc"] if a in owed]
        if not direct and sched["cc"]:
            direct, sched["cc"] = sched["cc"], []
    base = _base_url(branding)
    view_url = f"{base}/api/v1/reports/shared/{row.token}" if base else None
    message = f"{row.email_subject}" + (f"\n{view_url}" if view_url else "")
    ctx = _channel_ctx(row.email_subject, message, getattr(branding, "display_name", "ZenPlus"), [],
                       view_url, row.schedule_id, row.title)
    delivered, errors, failed, permanent = await _deliver(
        db, sched, channel_ids=channel_ids, direct=direct, subject=row.email_subject,
        email_text=row.email_text or "", email_html=row.email_html, attachment=attachment,
        inline_images=inline_images, channel_message=message, ctx=ctx)

    all_delivered = list(row.delivered_to or []) + [d for d in delivered if d not in (row.delivered_to or [])]
    notes = list(row.notes or []) + [f"{NOT_DELIVERED_PREFIX}{p}" for p in permanent]
    remaining = failed
    next_retry = None
    if remaining and attempt < settings["retry_attempts"]:
        next_retry = datetime.now(timezone.utc) + timedelta(
            minutes=report_settings.retry_delay_minutes(attempt + 1))
    elif remaining:
        notes.append(f"Delivery to {len(remaining)} target(s) gave up after {attempt} retr"
                     f"{'y' if attempt == 1 else 'ies'}.")
    blocking_notes = [n for n in notes if n != NO_DATA_NOTE and not n.startswith("Retry ")]
    still_undelivered = [n[len(NOT_DELIVERED_PREFIX):] for n in notes if n.startswith(NOT_DELIVERED_PREFIX)]
    if not remaining:
        status = "partial" if blocking_notes or errors else "success"
    else:
        status = "partial" if all_delivered else "failed"
    retry_line = (f"Retry {attempt}{' (manual)' if manual else ''}: "
                  + ("delivered to " + ", ".join(delivered) if delivered else "nothing delivered")
                  + (f"; {'; '.join(errors)}" if errors else ""))
    await db.execute(
        text("""UPDATE report_runs
                SET delivered_to = CAST(:d AS jsonb), failed_targets = CAST(:ft AS jsonb),
                    retry_count = :rc, next_retry_at = :nr, status = :st, notes = CAST(:n AS jsonb),
                    error = :err
                WHERE id = :id"""),
        {"d": json.dumps(all_delivered), "ft": json.dumps(remaining), "rc": attempt, "nr": next_retry,
         "st": status, "n": json.dumps(notes + [retry_line]),
         "err": ("; ".join(errors) if remaining and errors else (row.error if remaining else
                 ("; ".join(still_undelivered) or None))),
         "id": row.id})
    if row.schedule_id:
        # The schedule's last status follows its latest run.
        await db.execute(text(
            """UPDATE report_schedules SET last_status = :st, last_error = :err
               WHERE id = :sid AND NOT EXISTS (
                   SELECT 1 FROM report_runs r2 WHERE r2.schedule_id = :sid
                     AND r2.generated_at > (SELECT generated_at FROM report_runs WHERE id = :rid))"""),
            {"st": status, "err": ("; ".join(errors) if remaining and errors
                                   else ("; ".join(still_undelivered) or None)),
             "sid": row.schedule_id, "rid": row.id})
    await db.commit()
    return {"status": status, "delivered": delivered, "errors": errors, "remaining": len(remaining),
            "next_retry_at": next_retry.isoformat() if next_retry else None}


# ---------------------------------------------------------------------------
# Tick loop
# ---------------------------------------------------------------------------

def _row_to_sched(row) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "enabled": row.enabled,
        "report_type": row.report_type,
        "period": row.period,
        "format": row.format,
        "filters": row.filters or {},
        "frequency": row.frequency,
        "hour": row.hour,
        "minute": row.minute,
        "day_of_week": row.day_of_week,
        "day_of_month": row.day_of_month,
        # The zone hour/minute are in (migrate-163); None before the migration.
        "timezone": getattr(row, "timezone", None),
        "notify_channels": row.notify_channels or [],
        # Custom (template) schedules render by this id; without it every
        # timer-fired custom schedule failed and only "Run now" worked.
        "custom_report_id": getattr(row, "custom_report_id", None),
        # Direct email recipients (migrate-156); absent before the migration.
        "recipients": list(getattr(row, "recipients", None) or []),
        # Email options and empty-report handling (migrate-166).
        "skip_when_empty": bool(getattr(row, "skip_when_empty", False)),
        "subject_template": getattr(row, "subject_template", None),
        "reply_to": getattr(row, "reply_to", None),
        "group_send": bool(getattr(row, "group_send", False)),
        "cc": list(getattr(row, "cc", None) or []),
    }


MAX_RUNS_PER_TICK = 100


async def run_scheduler_tick(db: AsyncSession) -> dict:
    """Run every due schedule, one at a time.

    Each schedule is claimed on its own (row lock, next_run_at advanced,
    marked running, committed) and only then generated: both API workers run
    this loop, so the claim keeps a slow run from being delivered twice, and
    claiming one at a time means a worker that dies mid-batch loses at most the
    run in progress — the other due schedules stay due for either worker."""
    now = datetime.now(timezone.utc)
    # Only the fallback for a schedule without its own valid timezone.
    tz_name = await get_configured_timezone(db)
    fired: list[str] = []
    done: list[str] = []
    while len(done) < MAX_RUNS_PER_TICK:
        row = (await db.execute(
            text("""SELECT * FROM report_schedules
                    WHERE enabled = TRUE AND next_run_at IS NOT NULL AND next_run_at <= :now
                      AND NOT (id::text = ANY(:done))
                      -- not while a "Run now" of the same schedule is in progress
                      AND (last_status IS DISTINCT FROM 'running' OR updated_at IS NULL
                           OR updated_at <= :now - INTERVAL '30 minutes')
                    ORDER BY next_run_at
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED"""),
            {"now": now, "done": done},
        )).first()
        if row is None or str(row.id) in done:
            break
        sched = _row_to_sched(row)
        done.append(sched["id"])
        nxt = compute_next_run(sched, tz_name, after=now + timedelta(seconds=1))
        await db.execute(
            text("""UPDATE report_schedules
                    SET last_run_at = :now, next_run_at = :nxt, last_status = 'running',
                        last_error = NULL, updated_at = NOW()
                    WHERE id = :id"""),
            {"now": now, "nxt": nxt, "id": sched["id"]},
        )
        await db.commit()

        try:
            result = await generate_and_deliver(db, sched)
            last_status = result["status"]
            last_error = "; ".join(result["errors"]) if result["errors"] else None
        except Exception as exc:
            logger.exception("report schedule %s failed", sched["id"])
            await db.rollback()
            last_status, last_error = "failed", short_error(exc)
        await db.execute(
            text("""UPDATE report_schedules
                    SET last_status = :st, last_error = :err, updated_at = NOW()
                    WHERE id = :id"""),
            {"st": last_status, "err": last_error, "id": sched["id"]},
        )
        await db.commit()
        fired.append(sched["id"])

    if fired:
        logger.info("report scheduler tick: fired %d schedule(s)", len(fired))
    retried = await run_due_retries(db, now)
    await _housekeeping(db, now)
    return {"fired": fired, "checked": len(done), "retried": retried}


MAX_RETRIES_PER_TICK = 20
STALE_RUN_MINUTES = 30
HEARTBEAT_KEY = "report_scheduler"
_last_retention_at: Optional[datetime] = None


async def run_due_retries(db: AsyncSession, now: datetime) -> list[str]:
    """Retry the runs whose ``next_retry_at`` has come, one at a time; each is
    claimed (next_retry_at cleared, committed) first so the other API worker
    never retries it in parallel."""
    done: list[str] = []
    while len(done) < MAX_RETRIES_PER_TICK:
        row = (await db.execute(text(
            """SELECT id FROM report_runs
               WHERE next_retry_at IS NOT NULL AND next_retry_at <= :now
               ORDER BY next_retry_at LIMIT 1 FOR UPDATE SKIP LOCKED"""), {"now": now})).first()
        if row is None:
            break
        run_id = str(row.id)
        await db.execute(text("UPDATE report_runs SET next_retry_at = NULL WHERE id = :id"), {"id": row.id})
        await db.commit()
        done.append(run_id)
        try:
            await retry_run(db, run_id)
        except Exception:
            logger.exception("report run %s retry failed", run_id)
            await db.rollback()
    return done


async def _housekeeping(db: AsyncSession, now: datetime) -> None:
    """Heartbeat, runs left "running" by a dead worker, and (hourly) the
    run/artifact retention of the report settings."""
    global _last_retention_at
    from app.services import report_settings
    try:
        await db.execute(
            text("INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb)) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
            {"k": HEARTBEAT_KEY, "v": json.dumps({"last_tick_at": now.isoformat()})})
        stale = now - timedelta(minutes=STALE_RUN_MINUTES)
        await db.execute(text(
            "UPDATE report_runs SET status = 'failed', "
            "error = concat_ws('; ', error, 'Generation did not finish (the worker stopped)') "
            "WHERE status = 'running' AND generated_at < :stale"), {"stale": stale})
        await db.execute(text(
            "UPDATE report_schedules SET last_status = 'failed', "
            "last_error = 'The last run did not finish (the worker stopped)' "
            "WHERE last_status = 'running' AND updated_at < :stale"), {"stale": stale})
        if _last_retention_at is None or now - _last_retention_at >= timedelta(hours=1):
            settings = await report_settings.load(db)
            await db.execute(text(
                "DELETE FROM report_runs WHERE generated_at < :cut AND status <> 'running'"),
                {"cut": now - timedelta(days=settings["run_retention_days"])})
            await db.execute(text(
                "UPDATE report_runs SET artifact = NULL, email_html = NULL, email_text = NULL, "
                "failed_targets = '[]'::jsonb, next_retry_at = NULL "
                "WHERE generated_at < :cut AND (artifact IS NOT NULL OR email_html IS NOT NULL)"),
                {"cut": now - timedelta(days=settings["artifact_retention_days"])})
            _last_retention_at = now
        await db.commit()
    except Exception:
        logger.warning("report scheduler housekeeping failed", exc_info=True)
        await db.rollback()


async def scheduler_last_tick(db: AsyncSession) -> Optional[datetime]:
    row = (await db.execute(
        text("SELECT value FROM system_settings WHERE key = :k"), {"k": HEARTBEAT_KEY})).first()
    value = row[0] if row else None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    try:
        return datetime.fromisoformat(value["last_tick_at"]) if isinstance(value, dict) else None
    except (KeyError, TypeError, ValueError):
        return None


async def report_scheduler_loop() -> None:
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(20)  # let the app settle
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await run_scheduler_tick(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("report scheduler tick failed")
        await asyncio.sleep(TICK_INTERVAL_S)
