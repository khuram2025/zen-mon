"""Background scheduler for recurring report delivery.

A 60-second loop (started from main.py) fires every due, enabled
``report_schedules`` row: it generates the report data, renders a professional
HTML summary, stores a token-gated ``report_runs`` artifact (the "View full
report" share target), attaches the report as PDF/Excel/CSV, and emails it to
each linked notification channel. Next-run is computed in the appliance's
configured timezone.

Mirrors the discovery scheduler's shape (compute_next_run + tick loop) and
reuses the existing SMTP gateway resolution + report generators.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.alert_schedule import get_configured_timezone
from app.services.report_data_service import build_executive
from app.services.report_service import generate_report, _resolve_period
from app.services.export_service import generate_excel_report, generate_csv_report
from app.services import report_html

logger = logging.getLogger("zenplus.report_scheduler")

TICK_INTERVAL_S = 60


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
# SMTP resolution + send (with optional attachment)
# ---------------------------------------------------------------------------

async def _resolve_smtp(db: AsyncSession, channel_row) -> dict | None:
    """Return SMTP gateway config dict for an email channel, or None."""
    cfg = channel_row.config or {}
    gw_id = channel_row.gateway_id or cfg.get("gateway_id")
    if gw_id:
        gw = (await db.execute(
            text("SELECT config FROM notification_gateways WHERE id = :id"), {"id": gw_id}
        )).first()
    else:
        gw = (await db.execute(
            text("SELECT config FROM notification_gateways WHERE type='smtp' AND is_default=true LIMIT 1")
        )).first()
    gw_config = gw.config if gw else None
    if not gw_config:
        row = (await db.execute(
            text("SELECT value FROM system_settings WHERE key='smtp'")
        )).first()
        gw_config = row[0] if row and isinstance(row[0], dict) else None
    if not gw_config or not gw_config.get("host") or not gw_config.get("enabled", True):
        return None
    return gw_config


def _send_email_with_attachment(gw: dict, recipients: list[str], subject: str,
                                text_body: str, html_body: str,
                                attachment: tuple[str, bytes, str] | None = None) -> None:
    """Send a multipart email. attachment = (filename, bytes, mime_subtype)."""
    outer = MIMEMultipart("mixed")
    outer["From"] = f"{gw.get('from_name', 'ZenPlus')} <{gw.get('from_email', '')}>"
    outer["To"] = ", ".join(recipients)
    outer["Subject"] = subject

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body, "plain"))
    alt.attach(MIMEText(html_body, "html"))
    outer.attach(alt)

    if attachment:
        fname, payload, subtype = attachment
        part = MIMEApplication(payload, _subtype=subtype)
        part.add_header("Content-Disposition", "attachment", filename=fname)
        outer.attach(part)

    enc = gw.get("encryption", "tls")
    if enc == "ssl":
        server = smtplib.SMTP_SSL(gw["host"], int(gw.get("port", 465)), timeout=20)
    else:
        server = smtplib.SMTP(gw["host"], int(gw.get("port", 587)), timeout=20)
        if enc == "tls":
            server.starttls()
    if gw.get("username"):
        server.login(gw["username"], gw.get("password", ""))
    server.sendmail(gw.get("from_email", ""), recipients, outer.as_string())
    server.quit()


# ---------------------------------------------------------------------------
# Generation + delivery
# ---------------------------------------------------------------------------

def _base_url(db_company: dict | None) -> str:
    s = get_settings()
    base = (s.APP_BASE_URL or "").strip()
    if not base and isinstance(db_company, dict):
        base = (db_company.get("base_url") or "").strip()
    return base.rstrip("/")


async def generate_and_deliver(db: AsyncSession, schedule: dict, *, triggered_by: str = "scheduled") -> dict:
    """Render a report for ``schedule`` and email it to its channels.

    Returns a summary dict {run_id, token, delivered, errors}. Always records a
    report_runs row (even on partial delivery) so the share link works.
    """
    report_type = schedule["report_type"]
    period = schedule["period"]
    fmt = schedule.get("format", "pdf")
    filters = schedule.get("filters") or {}

    start, end, plabel = _resolve_period(period, None, None)

    from app.services import report_sections as _sections
    is_sections = report_type == "custom" or report_type in _sections.REPORT_PRESETS

    company_row = (await db.execute(
        text("SELECT value FROM system_settings WHERE key='company'")
    )).first()
    company = company_row[0] if company_row and isinstance(company_row[0], dict) else {}
    company_name = company.get("company_name") or company.get("name") or "ZenPlus"

    data = None
    secs: list[dict] = []
    sec_meta: dict = {}
    if is_sections:
        # Section-engine report types (availability/performance/traffic/…/custom).
        custom_id = schedule.get("custom_report_id")
        preset_title, _desc, section_ids = await _sections.resolve_report(
            db, "custom" if report_type == "custom" else report_type,
            str(custom_id) if custom_id else None)
        title = schedule.get("name") or preset_title
        secs = await _sections.build_sections(db, section_ids, start, end, filters or None)
        cat = (_sections.REPORT_PRESETS.get(report_type) or {}).get("category") or \
            ("Custom Report" if report_type == "custom" else "")
        dev_filter = (filters or {}).get("device_ids")
        sec_meta = await _sections.build_report_meta(
            db, title, start, end, description=_desc, category=cat,
            scope_label=(f"{len(dev_filter)} selected device(s)" if dev_filter
                         else "All monitored infrastructure"))
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
        "company_name": company_name,
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

    base = _base_url(company)
    view_url = f"{base}/api/v1/reports/shared/{token}" if base else None

    # Build the formal attachment.
    attachment = None
    if fmt != "none":
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
            safe = (meta["title"] or "Report").replace(" ", "-")[:40]
            if is_sections:
                # Section reports ship as PDF (excel/csv are legacy-only formats).
                if fmt == "pdf":
                    attachment = (f"ZenPlus-{safe}-{ts}.pdf",
                                  bytes(_sections.render_pdf(sec_meta, secs)), "pdf")
            else:
                common = dict(db=db, report_type=report_type, period=period,
                              from_time=None, to_time=None,
                              device_ids=filters.get("device_ids"),
                              group_ids=filters.get("group_ids"),
                              locations=filters.get("locations"),
                              device_types=filters.get("device_types"))
                if fmt == "excel":
                    payload = await generate_excel_report(**common)
                    attachment = (f"ZenPlus-{safe}-{ts}.xlsx", bytes(payload),
                                  "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                elif fmt == "csv":
                    payload = await generate_csv_report(**common)
                    if isinstance(payload, str):
                        payload = payload.encode("utf-8")
                    attachment = (f"ZenPlus-{safe}-{ts}.csv", bytes(payload), "csv")
                else:
                    attachment = (f"ZenPlus-{safe}-{ts}.pdf",
                                  bytes(await generate_report(**common)), "pdf")
        except Exception:
            logger.exception("report attachment generation failed (schedule %s)", schedule.get("id"))
            attachment = None

    if is_sections:
        email_html = _sections.email_summary_html(sec_meta, secs, view_url,
                                                  attached=attachment is not None)
        email_text = _sections.email_summary_text(sec_meta, secs, view_url)
    else:
        email_html = report_html.build_report_email_html(meta, data, view_url, attached=attachment is not None)
        email_text = report_html.build_report_email_text(meta, data, view_url)
    subject = f"[Report] {meta['title']} — {plabel}"

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
            cfg = ch.config or {}

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
                _send_email_with_attachment(gw, recipients, subject, email_text, email_html, attachment)
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
                "hostname": company_name, "ip_address": "",
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

    # No channels linked just means the artifact was generated with nothing to
    # deliver — that's a success, not a failure.
    if not channel_ids:
        status = "success"
    elif delivered and not errors:
        status = "success"
    elif delivered:
        status = "partial"
    else:
        status = "failed"
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

    fired: list[str] = []
    for row in rows:
        sched = _row_to_sched(row)
        try:
            result = await generate_and_deliver(db, sched)
            last_status = result["status"]
            last_error = "; ".join(result["errors"]) if result["errors"] else None
        except Exception as exc:
            logger.exception("report schedule %s failed", sched["id"])
            last_status, last_error = "failed", str(exc)
        nxt = compute_next_run(sched, tz_name, after=now + timedelta(seconds=1))
        await db.execute(
            text("""UPDATE report_schedules
                    SET last_run_at = :now, last_status = :st, last_error = :err,
                        next_run_at = :nxt, updated_at = NOW()
                    WHERE id = :id"""),
            {"now": now, "st": last_status, "err": last_error, "nxt": nxt, "id": sched["id"]},
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
