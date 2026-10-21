"""Professional HTML rendering for scheduled reports.

Two outputs, one shared visual language (matching ``email_render``):

* ``build_report_html(meta, data)`` — a standalone, self-contained HTML page
  (the "View full report" target served by the token-gated share endpoint).
* ``build_report_email_html(meta, data, view_url)`` /
  ``build_report_email_text(...)`` — the email body: a polished KPI summary with
  a prominent "View full report" button (and the full report attached as PDF).

All HTML is table + inline-CSS only so it renders consistently in email clients
and any browser. Input ``data`` is the executive dataset produced by
``report_data_service.build_executive`` (KPIs, top issues, location summary,
availability trend, outage timeline); missing keys degrade gracefully.

Branding comes from ``meta``: ``branding`` (a branding.Branding — company
name, brand colour, timezone and date/time format, footer and disclaimer),
or the individual ``company_name`` / ``brand_color`` / ``generated_label``
keys. ``start``/``end`` (aware datetimes) add the exact window, in the
organisation's timezone, to a relative period label. ``logo_cid`` (email) or
``logo_data_uri`` (standalone page) shows the logo; the company name is always
shown as text.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.services import report_colors as rc
from app.services.branding import DEFAULT_BRAND_COLOR, Branding

# Shared palette (kept in sync with email_render.py)
_INK = "#0F172A"
_MUTED = "#64748B"
_BORDER = "#E2E8F0"
_BG = "#F1F5F9"
_CARD = "#FFFFFF"
_BRAND = DEFAULT_BRAND_COLOR  # fallback only; the organisation's colour wins
_GOOD = "#16A34A"
_WARN = "#F59E0B"
_BAD = "#DC2626"
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

_REPORT_TITLES = {
    "executive_summary": "Executive Summary",
    "device_health": "Device Health Report",
    "service_health": "Service Health Report",
    "alert_analysis": "Alert Analysis Report",
    "full_report": "Full Network Report",
}

_PERIOD_LABELS = {
    "last_24h": "Last 24 hours",
    "last_7d": "Last 7 days",
    "last_30d": "Last 30 days",
}


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def report_title(report_type: str) -> str:
    return _REPORT_TITLES.get(report_type, "ZenPlus Report")


def period_label(period: str) -> str:
    return _PERIOD_LABELS.get(period, period or "")


def _fmt_ts(ts) -> str:
    """UTC fallback when no organisation branding is available."""
    if not ts:
        ts = datetime.now(timezone.utc)
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return str(ts)
    try:
        utc = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)
        return utc.strftime("%b %d, %Y · %H:%M UTC")
    except Exception:
        return str(ts)


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------

def _branding(meta: dict) -> Optional[Branding]:
    b = (meta or {}).get("branding")
    return b if isinstance(b, Branding) else None


def _brand_hex(meta: dict) -> str:
    b = _branding(meta)
    return rc.normalize((meta or {}).get("brand_color") or (b.brand_color if b else None), _BRAND)


def _company(meta: dict) -> str:
    b = _branding(meta)
    return str((meta or {}).get("company_name") or (b.display_name if b else "") or "ZenPlus")


def _as_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _generated_text(meta: dict) -> str:
    """Generation time in the organisation's timezone and format with an
    unambiguous zone label ('GMT+3'); UTC when no branding is known."""
    meta = meta or {}
    if meta.get("generated_label"):
        return str(meta["generated_label"])
    b = _branding(meta)
    gen = _as_dt(meta.get("generated_at")) or datetime.now(timezone.utc)
    if b is not None:
        return b.fmt_datetime(gen, with_tz=True)
    return _fmt_ts(gen)


def _period_text(meta: dict) -> str:
    """The period label, plus the exact window in the organisation's timezone
    when ``start``/``end`` are known ('Last 7 days (01/10/2026 08:00 —
    08/10/2026 08:00 GMT+3)')."""
    meta = meta or {}
    base = str(meta.get("period_label") or period_label(meta.get("period", "")))
    b = _branding(meta)
    start, end = _as_dt(meta.get("start")), _as_dt(meta.get("end"))
    if b is None or start is None or end is None:
        return base
    window = b.period_label(start, end)
    if not base or base == window:
        return window
    return f"{base} ({window})"


_PAGE_LOGO_RE = re.compile(r"^data:image/(png|jpeg|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$")


def _logo_html(meta: dict, *, email: bool) -> str:
    """Logo cell for the header, or '' (the company name is always text).
    Email references a CID attachment only (Gmail and older Outlook block
    data: URIs); the standalone page may inline the logo."""
    meta = meta or {}
    if email:
        cid = meta.get("logo_cid")
        src = f"cid:{cid}" if isinstance(cid, str) and cid else ""
    else:
        uri = meta.get("logo_data_uri")
        src = uri if isinstance(uri, str) and _PAGE_LOGO_RE.match(uri) else ""
    if not src:
        return ""
    return (
        f'<td width="1" valign="middle" style="padding-right:12px;">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td bgcolor="#FFFFFF" style="background:#FFFFFF;border:1px solid {_BORDER};'
        f'border-radius:6px;padding:4px 8px;">'
        f'<img src="{html.escape(src, quote=True)}" alt="{html.escape(_company(meta), quote=True)}" '
        f'height="32" style="display:block;height:32px;width:auto;max-width:160px;border:0;'
        f'font-family:{_FONT};font-size:13px;font-weight:700;color:{_INK};"></td></tr></table></td>'
    )


def _num(v, suffix="", dash="—") -> str:
    if v is None:
        return dash
    if isinstance(v, float):
        v = round(v, 1)
    return f"{v}{suffix}"


def _delta_badge(delta, *, good_when_negative=False) -> str:
    """A small ▲/▼ delta chip. good_when_negative flips colour semantics
    (e.g. fewer incidents / lower MTTR is good)."""
    if delta in (None, 0):
        return ""
    up = delta > 0
    positive = (not up) if good_when_negative else up
    color = _GOOD if positive else _BAD
    arrow = "&#9650;" if up else "&#9660;"
    return (
        f'<span style="color:{color};font-size:12px;font-weight:600;margin-left:6px;">'
        f'{arrow} {abs(round(delta, 1))}</span>'
    )


# ---------------------------------------------------------------------------
# KPI extraction (tolerant)
# ---------------------------------------------------------------------------

def _kpi_cards(data: dict) -> list[dict]:
    k = (data or {}).get("kpis", {}) or {}
    avail = k.get("availability_pct")
    return [
        {
            "label": "Availability",
            "value": _num(avail, "%"),
            "delta": _delta_badge(k.get("availability_delta_pct")),
            "tone": (_MUTED if avail is None else
                     _GOOD if avail >= 99 else _WARN if avail >= 95 else _BAD),
        },
        {
            "label": "Devices Monitored",
            "value": _num(k.get("devices_monitored")),
            "delta": "",
            "tone": None,  # brand colour
        },
        {
            "label": "Active Critical",
            "value": _num(k.get("active_critical_count")),
            "delta": "",
            "tone": _BAD if (k.get("active_critical_count") or 0) else _GOOD,
        },
        {
            "label": "Incidents",
            "value": _num(k.get("incidents_count")),
            "delta": _delta_badge(k.get("incidents_delta"), good_when_negative=True),
            "tone": _INK,
        },
        {
            "label": "MTTR (min)",
            "value": _num(k.get("mttr_minutes")),
            "delta": _delta_badge(k.get("mttr_delta_minutes"), good_when_negative=True),
            "tone": _INK,
        },
        {
            "label": "SLA Attained",
            "value": _num(k.get("sla_attained_pct"), "%"),
            "delta": "",
            "tone": (_MUTED if k.get("sla_attained_pct") is None else
                     _GOOD if k["sla_attained_pct"] >= (k.get("sla_target_pct") or 99.9) else _WARN),
        },
    ]


def _kpi_grid_html(data: dict, brand: str = _BRAND) -> str:
    cards = _kpi_cards(data)
    cells = []
    for c in cards:
        tone = c["tone"] or rc.ink(brand)
        cells.append(
            f'<td width="33%" style="padding:6px;" valign="top">'
            f'<div style="border:1px solid {_BORDER};border-radius:10px;padding:14px 16px;background:{_CARD};">'
            f'<div style="font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;color:{_MUTED};">{_esc(c["label"])}</div>'
            f'<div style="margin-top:6px;font-size:24px;font-weight:700;color:{tone};">{c["value"]}{c["delta"]}</div>'
            f'</div></td>'
        )
    rows = []
    for i in range(0, len(cells), 3):
        rows.append("<tr>" + "".join(cells[i:i + 3]) + "</tr>")
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:separate;">' + "".join(rows) + "</table>"
    )


def _section_title(text: str) -> str:
    return (
        f'<div style="font-size:14px;font-weight:700;color:{_INK};'
        f'margin:22px 0 10px;">{_esc(text)}</div>'
    )


def _top_issues_html(data: dict, brand: str = _BRAND) -> str:
    issues = (data or {}).get("top_issues") or []
    if not issues:
        return ""
    sev_colors = {"critical": _BAD, "warning": _WARN, "info": brand}
    rows = [
        f'<tr style="background:{_BG};"><th align="left" style="padding:8px 12px;font-size:11px;'
        f'text-transform:uppercase;color:{_MUTED};letter-spacing:.4px;">Device</th>'
        f'<th align="left" style="padding:8px 12px;font-size:11px;text-transform:uppercase;color:{_MUTED};">Issue</th>'
        f'<th align="right" style="padding:8px 12px;font-size:11px;text-transform:uppercase;color:{_MUTED};">Downtime</th>'
        f'<th align="right" style="padding:8px 12px;font-size:11px;text-transform:uppercase;color:{_MUTED};">Alerts</th></tr>'
    ]
    for it in issues[:8]:
        sev = sev_colors.get((it.get("severity") or "info").lower(), brand)
        rows.append(
            f'<tr style="border-top:1px solid {_BORDER};">'
            f'<td style="padding:9px 12px;font-size:13px;font-weight:600;color:{_INK};">{_esc(it.get("hostname","Unknown"))}</td>'
            f'<td style="padding:9px 12px;font-size:13px;">'
            f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;'
            f'font-weight:600;color:{rc.text_on(sev)};background:{sev};">{_esc((it.get("issue") or "").upper() or "—")}</span></td>'
            f'<td align="right" style="padding:9px 12px;font-size:13px;color:{_INK};">{_num(it.get("duration_minutes"),"m")}</td>'
            f'<td align="right" style="padding:9px 12px;font-size:13px;color:{_INK};">{_num(it.get("alert_count"))}</td>'
            f'</tr>'
        )
    return (
        _section_title("Top Issues")
        + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border:1px solid {_BORDER};border-radius:10px;border-collapse:separate;overflow:hidden;">'
        + "".join(rows) + "</table>"
    )


def _locations_html(data: dict) -> str:
    locs = (data or {}).get("location_summary") or []
    if not locs:
        return ""
    rows = [
        f'<tr style="background:{_BG};"><th align="left" style="padding:8px 12px;font-size:11px;'
        f'text-transform:uppercase;color:{_MUTED};letter-spacing:.4px;">Location</th>'
        f'<th align="right" style="padding:8px 12px;font-size:11px;text-transform:uppercase;color:{_MUTED};">Devices</th>'
        f'<th align="right" style="padding:8px 12px;font-size:11px;text-transform:uppercase;color:{_MUTED};">Down</th>'
        f'<th align="right" style="padding:8px 12px;font-size:11px;text-transform:uppercase;color:{_MUTED};">Availability</th></tr>'
    ]
    for lc in locs[:10]:
        av = lc.get("availability_pct")
        av_color = _MUTED if av is None else (_GOOD if av >= 99 else (_WARN if av >= 95 else _BAD))
        rows.append(
            f'<tr style="border-top:1px solid {_BORDER};">'
            f'<td style="padding:9px 12px;font-size:13px;font-weight:600;color:{_INK};">{_esc(lc.get("location","Unknown"))}</td>'
            f'<td align="right" style="padding:9px 12px;font-size:13px;color:{_INK};">{_num(lc.get("devices"))}</td>'
            f'<td align="right" style="padding:9px 12px;font-size:13px;color:{_BAD if (lc.get("down") or 0) else _MUTED};">{_num(lc.get("down"))}</td>'
            f'<td align="right" style="padding:9px 12px;font-size:13px;font-weight:600;color:{av_color};">{_num(av,"%")}</td>'
            f'</tr>'
        )
    return (
        _section_title("Availability by Location")
        + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border:1px solid {_BORDER};border-radius:10px;border-collapse:separate;overflow:hidden;">'
        + "".join(rows) + "</table>"
    )


def _header_block(meta: dict, *, status_chip: str | None = None, email: bool = False) -> str:
    brand = _brand_hex(meta)
    company = _esc(_company(meta))
    title = _esc(meta.get("title") or report_title(meta.get("report_type", "")))
    plabel = _esc(_period_text(meta))
    gen = _generated_text(meta)
    tagline = _esc(meta.get("category") or "Network Monitoring")
    chip = ""
    if status_chip:
        chip = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="right"><tr>'
            f'<td bgcolor="{brand}" style="background:{brand};border-radius:999px;padding:5px 11px;'
            f'color:{rc.text_on(brand)};font-size:11px;font-weight:700;letter-spacing:.6px;">'
            f'{_esc(status_chip)}</td></tr></table>'
        )
    return (
        f'<tr><td bgcolor="{brand}" style="height:6px;background:{brand};font-size:0;line-height:0;">&nbsp;</td></tr>'
        f'<tr><td style="padding:20px 28px 4px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'{_logo_html(meta, email=email)}'
        f'<td valign="middle">'
        f'<div style="font-size:16px;font-weight:700;color:{_INK};letter-spacing:.2px;">{company}</div>'
        f'<div style="font-size:10px;font-weight:600;color:{_MUTED};letter-spacing:.8px;'
        f'text-transform:uppercase;">{tagline}</div></td>'
        f'<td align="right" valign="middle">{chip}</td></tr></table></td></tr>'
        f'<tr><td style="padding:8px 28px 2px;">'
        f'<div style="font-size:22px;line-height:1.25;font-weight:700;color:{_INK};">{title}</div>'
        f'<div style="margin-top:4px;font-size:13px;color:{_MUTED};">{plabel} &middot; generated {_esc(gen)}</div>'
        f'</td></tr>'
    )


def _footer_block(meta: dict, *, email: bool) -> str:
    company = _esc(_company(meta))
    b = _branding(meta)
    footer_text = str(meta.get("footer_text") or (b.report_footer if b else "") or "").strip()
    disclaimer = str(meta.get("disclaimer") or (b.report_disclaimer if b else "") or "").strip()
    contact = str(meta.get("contact_line") or (b.contact_line if b else "") or "").strip()
    if email:
        schedule = str(meta.get("schedule_name") or "").strip()
        why = (f"You receive this report as a recipient of the report schedule “{_esc(schedule)}”."
               if schedule else "You receive this report as a recipient of a report schedule.")
        when = (" It was generated on request (manual run), not at the schedule's time."
                if meta.get("run_kind") == "manual" else "")
        note = why + when
    else:
        note = "Automatically generated report. Figures cover the period shown above."
    extra = ""
    if footer_text or contact:
        extra = "<br>" + _esc(" · ".join(p for p in (footer_text, contact) if p))
    tail = " &middot; This mailbox is not monitored." if email else ""
    disc = (f'<div style="margin-top:8px;color:{_MUTED};font-size:10px;line-height:1.45;">'
            f'{_esc(disclaimer)}</div>' if disclaimer else "")
    return (
        f'<tr><td style="padding:18px 28px 26px;">'
        f'<div style="border-top:1px solid {_BORDER};padding-top:14px;color:{_MUTED};font-size:12px;line-height:1.5;">'
        f'{note}{extra}<br>&copy; {datetime.now(timezone.utc).year} {company}{tail}</div>{disc}'
        f'</td></tr>'
    )


def _shell(inner: str, *, title: str, width: int = 720, preheader: str = "") -> str:
    pre = ""
    if preheader:
        pre = (f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;">'
               f'{_esc(preheader)}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>')
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta name="x-apple-disable-message-reformatting">
<title>{_esc(title)}</title></head>
<body bgcolor="{_BG}" style="margin:0;padding:0;background:{_BG};">
{pre}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{_BG}" style="background:{_BG};padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="{width}" cellpadding="0" cellspacing="0" bgcolor="{_CARD}" style="max-width:{width}px;width:100%;background:{_CARD};border:1px solid {_BORDER};border-radius:14px;overflow:hidden;font-family:{_FONT};">
{inner}
</table>
</td></tr></table>
</body></html>"""


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

def build_report_html(meta: dict, data: dict) -> str:
    """Standalone full HTML report page (token share link target)."""
    brand = _brand_hex(meta)
    inner = (
        _header_block(meta, status_chip="REPORT")
        + f'<tr><td style="padding:18px 22px 0;">{_kpi_grid_html(data, brand)}</td></tr>'
        + f'<tr><td style="padding:0 28px;">{_top_issues_html(data, brand)}</td></tr>'
        + f'<tr><td style="padding:0 28px;">{_locations_html(data)}</td></tr>'
        + _footer_block(meta, email=False)
    )
    return _shell(inner, title=meta.get("title") or report_title(meta.get("report_type", "")))


def build_report_email_html(meta: dict, data: dict, view_url: str | None = None,
                            attached: bool = False) -> str:
    """Email body: KPI summary + prominent 'View full report' button."""
    brand = _brand_hex(meta)
    button = ""
    if view_url and re.match(r"^https?://", str(view_url), re.IGNORECASE):
        button = (
            f'<tr><td style="padding:6px 28px 4px;">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td bgcolor="{brand}" style="background:{brand};border-radius:8px;">'
            f'<a href="{html.escape(str(view_url), quote=True)}" style="display:inline-block;'
            f'color:{rc.text_on(brand)};text-decoration:none;font-size:14px;font-weight:600;'
            f'padding:12px 24px;font-family:{_FONT};">View full report &rarr;</a></td></tr></table>'
            f'</td></tr>'
        )
    attach_note = ""
    if attached:
        attach_note = (
            f'<tr><td style="padding:4px 28px 0;">'
            f'<div style="font-size:12px;color:{_MUTED};">The full report is attached to this email.</div>'
            f'</td></tr>'
        )
    if meta.get("delivery_note"):
        attach_note += (
            f'<tr><td style="padding:4px 28px 0;">'
            f'<div style="font-size:12px;color:{_MUTED};">{_esc(meta["delivery_note"])}</div>'
            f'</td></tr>'
        )
    run_note = ""
    if meta.get("run_note"):
        run_note = (
            f'<tr><td style="padding:10px 28px 0;">'
            f'<div style="background:#FEF3C7;color:#92400E;border-radius:8px;padding:8px 12px;'
            f'font-size:12px;">{_esc(meta["run_note"])}</div></td></tr>'
        )
    k = (data or {}).get("kpis", {}) or {}
    pre_bits = []
    if k.get("availability_pct") is not None:
        pre_bits.append(f"Availability {_num(k.get('availability_pct'), '%')}")
    if k.get("incidents_count") is not None:
        pre_bits.append(f"{_num(k.get('incidents_count'))} incidents")
    if k.get("mttr_minutes") is not None:
        pre_bits.append(f"MTTR {_num(k.get('mttr_minutes'), 'm')}")
    inner = (
        _header_block(meta, status_chip="MANUAL RUN" if meta.get("run_kind") == "manual"
                      else "SCHEDULED REPORT", email=True)
        + run_note
        + f'<tr><td style="padding:16px 22px 0;">{_kpi_grid_html(data, brand)}</td></tr>'
        + f'<tr><td style="padding:6px 28px 0;">{_top_issues_html(data, brand)}</td></tr>'
        + button
        + attach_note
        + _footer_block(meta, email=True)
    )
    return _shell(inner, title=meta.get("title") or report_title(meta.get("report_type", "")),
                  width=640, preheader=" · ".join(pre_bits))


def build_report_email_text(meta: dict, data: dict, view_url: str | None = None) -> str:
    k = (data or {}).get("kpis", {}) or {}
    title = meta.get("title") or report_title(meta.get("report_type", ""))
    company = _company(meta)
    lines = [
        f"{title}",
        f"{company} · {_period_text(meta)}",
        f"Generated {_generated_text(meta)}",
        "",
        f"Availability:    {_num(k.get('availability_pct'), '%')}",
        f"Devices:         {_num(k.get('devices_monitored'))}",
        f"Active critical:  {_num(k.get('active_critical_count'))}",
        f"Incidents:       {_num(k.get('incidents_count'))}",
        f"MTTR (min):      {_num(k.get('mttr_minutes'))}",
        f"SLA attained:    {_num(k.get('sla_attained_pct'), '%')}",
    ]
    issues = (data or {}).get("top_issues") or []
    if issues:
        lines += ["", "Top issues:"]
        for it in issues[:5]:
            lines.append(
                f"  - {it.get('hostname','Unknown')}: {(it.get('issue') or '').upper()} "
                f"({_num(it.get('duration_minutes'),'m')}, {_num(it.get('alert_count'))} alerts)"
            )
    if meta.get("run_note"):
        lines += ["", str(meta["run_note"])]
    if view_url:
        lines += ["", f"View full report: {view_url}"]
    if meta.get("delivery_note"):
        lines += ["", str(meta["delivery_note"])]
    lines += ["", f"— {company} automated report"]
    b = _branding(meta)
    disclaimer = str(meta.get("disclaimer") or (b.report_disclaimer if b else "") or "").strip()
    if disclaimer:
        lines.append(disclaimer)
    return "\n".join(lines)
