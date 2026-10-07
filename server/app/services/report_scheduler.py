"""Background scheduler for recurring report delivery.

A 60-second loop (started from main.py) fires every due, enabled
``report_schedules`` row. Each run:

* resolves its window with ``report_periods.resolve_period`` in the
  organisation timezone and week start (Settings -> General -> Company), so
  "Previous month" is the local calendar month, as in the report viewer;
* renders built-in templates (``report_sections.REPORT_PRESETS``) and
  organisation templates (``custom_reports``) with the section engine the way
  the viewer does, as the schedule's owner; legacy report types keep their
  executive-summary pipeline;
* stores a token-gated ``report_runs`` artifact (the "View full report" share
  target) and attaches the report as PDF, Excel or HTML (CSV for legacy types);
* emails a branded summary to every linked email channel and to the
  schedule's direct recipients; other channel types get a KPI line and the
  link.

Next-run is computed in the appliance's configured timezone. Mirrors the
discovery scheduler's shape (compute_next_run + tick loop) and reuses the SMTP
gateway resolution.
"""

from __future__ import annotations

import asyncio
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
from app.services.alert_schedule import get_configured_timezone
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
TEMPLATE_FORMATS = ("pdf", "excel", "html", "none")
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


def compute_next_run(sched: dict, tz_name: str, *, after: datetime | None = None) -> datetime | None:
    """Return the next fire time (UTC, tz-aware) for a schedule, or None.

    ``sched`` keys: frequency, hour, minute, day_of_week (1=Mon..7=Sun),
    day_of_month (1..31). Evaluated in the appliance timezone so "08:00 daily"
    means local 08:00.
    """
    if not sched.get("enabled", True):
        return None
    tz = _tz(tz_name)
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
                        inline_images: Iterable[tuple[str, bytes, str]] | None = None) -> MIMEMultipart:
    """multipart/mixed { alternative { text, related { html, inline images } }, attachment }.

    ``attachment`` = (filename, bytes, MIME type); ``inline_images`` =
    (content id, bytes, MIME type) referenced from the HTML as ``cid:<id>``."""
    outer = MIMEMultipart("mixed")
    outer["From"] = formataddr((_clean_header(str(gw.get("from_name") or "ZenPlus")),
                                str(gw.get("from_email") or "")), charset="utf-8")
    outer["To"] = ", ".join(recipients)
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
            server.starttls(context=ssl.create_default_context())
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
                                inline_images: Iterable[tuple[str, bytes, str]] | None = None) -> None:
    """Send one multipart email to ``recipients`` (an email channel's list).
    attachment = (filename, bytes, MIME type or bare application subtype)."""
    msg = build_email_message(gw, recipients, subject, text_body, html_body, attachment, inline_images)
    server = _smtp_connect(gw)
    try:
        server.sendmail(gw.get("from_email", ""), recipients, msg.as_string())
    finally:
        _smtp_close(server)


def _send_individually(gw: dict, recipients: list[str], subject: str, text_body: str, html_body: str,
                       attachment: tuple[str, bytes, str] | None = None,
                       inline_images: Iterable[tuple[str, bytes, str]] | None = None,
                       ) -> tuple[list[str], list[tuple[str, str]]]:
    """Send one message per direct recipient over a single SMTP session, so
    recipients (often external) never see each other's addresses. Returns
    (delivered addresses, [(address, error)]). Connection or login failures
    raise."""
    if not recipients:
        return [], []
    msg = build_email_message(gw, recipients[:1], subject, text_body, html_body, attachment, inline_images)
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
                failed.append((addr, _smtp_error(exc)))
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


def email_subject(branding: Branding | None, title: str, period_label: str) -> str:
    """'[Confidential] Monthly SLA — Previous month (1 Sep 2026 – 30 Sep 2026)'."""
    prefix = f"[{branding.confidentiality}] " if branding is not None and branding.confidentiality else ""
    return _clean_header(f"{prefix}{title} — {period_label}")


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
        return entries, filters, report_render.scope_label_for(filters)
    scoped, ids = await scoping.scoped_device_filter(db, owner, filters.get("device_ids"))
    if not scoped:
        return entries, filters, report_render.scope_label_for(filters)
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


async def generate_and_deliver(db: AsyncSession, schedule: dict, *, triggered_by: str = "scheduled") -> dict:
    """Render a report for ``schedule`` and deliver it to its channels and
    direct recipients.

    Returns a summary dict {run_id, token, view_url, delivered, errors,
    status}. Records a report_runs row (even on partial delivery) so the share
    link works; a schedule whose template is gone records nothing and fails
    with a clear error."""
    report_type = schedule["report_type"]
    period = schedule.get("period") or "last_24h"
    fmt = schedule.get("format") or "pdf"
    filters = {k: v for k, v in (schedule.get("filters") or {}).items() if k != "netflow_withheld"}

    branding = await load_branding(db)
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
    if not is_sections:
        from app.api.v1.netflow.access import VIEW_PERMISSION
        from app.services.netflow_report import flow_visibility_for

        netflow_withheld = not (await flow_visibility_for(db, owner)).can(VIEW_PERMISSION)
    if is_sections:
        try:
            default_title, description, category, entries = await _template_for(
                db, report_type, schedule.get("custom_report_id"), owner)
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
    else:
        # Legacy types: executive dataset drives the universal HTML/email summary.
        data = await build_executive(db, start, end)
        title = schedule.get("name") or report_html.report_title(report_type)

    meta = {
        "title": title,
        "report_type": report_type,
        "period": period,
        "period_label": plabel,
        "generated_at": datetime.now(timezone.utc),
        "company_name": branding.display_name,
        "product_name": "ZenPlus",
    }

    # Persist a shareable HTML artifact.
    token = secrets.token_urlsafe(24)
    if is_sections:
        html_page = _sections.render_html(sec_meta, secs)
    else:
        html_page = report_html.build_report_html(meta, data)
    run_row = (await db.execute(
        text("""INSERT INTO report_runs
                (schedule_id, report_type, period, title, token, html, status, generated_at)
                VALUES (:sid, :rt, :pd, :title, :token, :html, 'success', NOW())
                RETURNING id"""),
        {"sid": schedule.get("id"), "rt": report_type, "pd": period,
         "title": meta["title"], "token": token, "html": html_page},
    )).first()
    run_id = str(run_row.id)
    await db.commit()

    base = _base_url(branding)
    view_url = f"{base}/api/v1/reports/shared/{token}" if base else None

    # Build the formal attachment.
    attachment = None
    if fmt != "none":
        try:
            ext, mime = ATTACHMENT_TYPES.get(fmt) or ("", "")
            if not ext:
                raise _AttachmentUnavailable(f"Unknown attachment format '{fmt}'")
            if fmt == "html":
                payload = html_page.encode("utf-8")
            elif is_sections:
                if fmt == "pdf":
                    payload = bytes(_sections.render_pdf(sec_meta, secs))
                elif fmt == "excel":
                    from app.services import report_xlsx
                    payload = bytes(report_xlsx.render_xlsx(sec_meta, secs))
                else:
                    raise _AttachmentUnavailable(
                        f"{FORMAT_NAMES.get(fmt, fmt)} attachments are only available for legacy report "
                        "types; choose PDF, Excel or HTML")
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

    inline_images: list[tuple[str, bytes, str]] = []
    if is_sections:
        logo = _email_logo(_sections, sec_meta)
        extra: dict[str, Any] = {}
        if logo:
            inline_images.append(logo)
            extra["logo_cid"] = logo[0]
        email_html = _sections.email_summary_html(sec_meta, secs, view_url,
                                                  attached=attachment is not None, **extra)
        email_text = _sections.email_summary_text(sec_meta, secs, view_url)
    else:
        email_html = report_html.build_report_email_html(meta, data, view_url, attached=attachment is not None)
        email_text = report_html.build_report_email_text(meta, data, view_url)
    subject = email_subject(branding, meta["title"], plabel)

    # Compact KPI pairs for non-email channel payloads.
    kpi_pairs: list[tuple[str, str]] = []
    if is_sections:
        for s in secs:
            for k in (s.get("kpis") or []):
                kpi_pairs.append((str(k.get("label") or ""), str(k.get("value") or "")))
            if len(kpi_pairs) >= 6:
                break
    elif data:
        k = data.get("kpis") or {}
        if k.get("availability_pct") is not None:
            kpi_pairs.append(("Availability", f"{k['availability_pct']:.2f}%"))
        kpi_pairs.append(("Incidents", str(k.get("incidents_count") or 0)))
        kpi_pairs.append(("Devices monitored", str(k.get("devices_monitored") or 0)))

    # Deliver to each linked channel.
    channel_ids = schedule.get("notify_channels") or []
    delivered: list[str] = []
    errors: list[str] = []
    for cid in channel_ids:
        try:
            ch = (await db.execute(
                text("SELECT id, name, type, config, enabled, gateway_id "
                     "FROM notification_channels WHERE id = :id"), {"id": cid}
            )).first()
            if not ch or not ch.enabled:
                errors.append(f"{cid}: channel missing/disabled")
                continue
            cfg = decrypt_config(ch.config)

            if ch.type == "email":
                # Email carries the full report as an attachment.
                recipients = [r.strip() for r in cfg.get("recipients", "").split(",") if r.strip()]
                if not recipients:
                    errors.append(f"{ch.name}: no recipients")
                    continue
                gw = await _resolve_smtp(db, ch)
                if not gw:
                    errors.append(f"{ch.name}: no SMTP gateway")
                    continue
                await asyncio.to_thread(_send_email_with_attachment, gw, recipients, subject, email_text,
                                        email_html, attachment, inline_images)
                delivered.append(ch.name)
                continue

            # Every other channel gets a KPI summary plus the share link.
            body_msg = f"{meta['title']} — {plabel}"
            if kpi_pairs:
                body_msg += "\n" + " · ".join(f"{l}: {v}" for l, v in kpi_pairs[:4])
            if view_url:
                body_msg += f"\n{view_url}"
            ctx = {
                "subject": subject, "message": body_msg, "body": body_msg,
                "hostname": branding.display_name, "ip_address": "",
                "status": "REPORT", "severity": "info",
                "details": kpi_pairs + ([("View report", view_url)] if view_url else []),
                "triggered_at": datetime.now(timezone.utc).isoformat(),
                "rule_id": str(schedule.get("id") or ""), "rule_name": meta["title"],
            }
            if ch.type == "sms":
                from app.api.v1.alert_engine import _send_sms
                from app.services.host_alert_service import _gateway_config
                phones = cfg.get("phone_numbers", "")
                gw = await _gateway_config(db, ch, "sms")
                if phones and gw:
                    await _send_sms(gw, phones, body_msg)
                    delivered.append(ch.name)
                else:
                    errors.append(f"{ch.name}: no phone numbers or SMS gateway")
            else:
                from app.api.v1.alert_engine import _dispatch_channel
                if await _dispatch_channel(ch.type, cfg, ctx):
                    delivered.append(ch.name)
                else:
                    errors.append(f"{ch.name}: unsupported channel type '{ch.type}'")
        except Exception as exc:
            logger.exception("report delivery to channel %s failed", cid)
            errors.append(f"{cid}: {exc}")

    # Deliver to the schedule's direct recipients through the default SMTP gateway.
    direct = [r for r in (str(x).strip().lower() for x in (schedule.get("recipients") or [])) if is_email(r)]
    direct = list(dict.fromkeys(direct))[:MAX_RECIPIENTS]
    if direct:
        try:
            gw = await _resolve_smtp_gateway(db)
            if not gw:
                errors.append(f"{len(direct)} direct recipient(s): no SMTP gateway is configured")
            else:
                sent, failed = await asyncio.to_thread(_send_individually, gw, direct, subject, email_text,
                                                       email_html, attachment, inline_images)
                delivered.extend(sent)
                errors.extend(f"{addr}: {err}" for addr, err in failed)
        except Exception as exc:
            logger.exception("report delivery to direct recipients failed (schedule %s)", schedule.get("id"))
            errors.append(f"{len(direct)} direct recipient(s): {_smtp_error(exc)}")

    # No delivery targets just means the artifact was generated with nothing
    # to deliver — that's a success, not a failure.
    if not channel_ids and not direct:
        status = "success"
    elif delivered and not errors:
        status = "success"
    elif delivered:
        status = "partial"
    else:
        status = "failed"
    if run_notes:
        errors.extend(run_notes)
        if status == "success":
            status = "partial"
    await db.execute(
        text("UPDATE report_runs SET delivered_to = CAST(:d AS jsonb), status = :st, error = :err WHERE id = :id"),
        {"d": json.dumps(delivered), "st": status,
         "err": "; ".join(errors) if errors else None, "id": run_id},
    )
    await db.commit()

    return {"run_id": run_id, "token": token, "view_url": view_url,
            "delivered": delivered, "errors": errors, "status": status}


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
        "notify_channels": row.notify_channels or [],
        # Custom (template) schedules render by this id; without it every
        # timer-fired custom schedule failed and only "Run now" worked.
        "custom_report_id": getattr(row, "custom_report_id", None),
        # Direct email recipients (migrate-156); absent before the migration.
        "recipients": list(getattr(row, "recipients", None) or []),
    }


async def run_scheduler_tick(db: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    tz_name = await get_configured_timezone(db)
    rows = (await db.execute(
        text("""SELECT * FROM report_schedules
                WHERE enabled = TRUE AND next_run_at IS NOT NULL AND next_run_at <= :now
                FOR UPDATE SKIP LOCKED"""),
        {"now": now},
    )).fetchall()

    # Claim every due row by advancing next_run_at while the row locks are
    # held. Generation commits (releasing the locks) part-way through, and both
    # API workers run this loop: without the claim a slow run could be picked
    # up again by the other worker and delivered twice.
    claimed: list[dict] = []
    for row in rows:
        sched = _row_to_sched(row)
        nxt = compute_next_run(sched, tz_name, after=now + timedelta(seconds=1))
        await db.execute(
            text("""UPDATE report_schedules
                    SET last_run_at = :now, next_run_at = :nxt, updated_at = NOW()
                    WHERE id = :id"""),
            {"now": now, "nxt": nxt, "id": sched["id"]},
        )
        claimed.append(sched)
    if claimed:
        await db.commit()

    fired: list[str] = []
    for sched in claimed:
        try:
            result = await generate_and_deliver(db, sched)
            last_status = result["status"]
            last_error = "; ".join(result["errors"]) if result["errors"] else None
        except Exception as exc:
            logger.exception("report schedule %s failed", sched["id"])
            await db.rollback()
            last_status, last_error = "failed", type(exc).__name__
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
    return {"fired": fired, "checked": len(rows)}


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
