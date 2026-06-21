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
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

# Shared palette (kept in sync with email_render.py)
_INK = "#0F172A"
_MUTED = "#64748B"
_BORDER = "#E2E8F0"
_BG = "#F1F5F9"
_CARD = "#FFFFFF"
_BRAND = "#2563EB"
_GOOD = "#16A34A"
_WARN = "#F59E0B"
_BAD = "#DC2626"

_SEV = {"critical": _BAD, "warning": _WARN, "info": _BRAND}

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
    if not ts:
        ts = datetime.now(timezone.utc)
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return _esc(ts)
    try:
        return ts.strftime("%b %d, %Y · %H:%M %Z").strip() or ts.strftime("%b %d, %Y · %H:%M UTC")
    except Exception:
        return _esc(str(ts))


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
            "tone": _GOOD if (avail or 0) >= 99 else (_WARN if (avail or 0) >= 95 else _BAD),
        },
        {
            "label": "Devices Monitored",
            "value": _num(k.get("devices_monitored")),
            "delta": "",
            "tone": _BRAND,
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
            "tone": _GOOD if (k.get("sla_attained_pct") or 0) >= (k.get("sla_target_pct") or 99.9) else _WARN,
        },
    ]


def _kpi_grid_html(data: dict) -> str:
    cards = _kpi_cards(data)
    cells = []
    for c in cards:
        cells.append(
            f'<td width="33%" style="padding:6px;" valign="top">'
            f'<div style="border:1px solid {_BORDER};border-radius:10px;padding:14px 16px;background:{_CARD};">'
            f'<div style="font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;color:{_MUTED};">{_esc(c["label"])}</div>'
            f'<div style="margin-top:6px;font-size:24px;font-weight:700;color:{c["tone"]};">{c["value"]}{c["delta"]}</div>'
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


def _top_issues_html(data: dict) -> str:
    issues = (data or {}).get("top_issues") or []
    if not issues:
        return ""
    rows = [
        f'<tr style="background:{_BG};"><th align="left" style="padding:8px 12px;font-size:11px;'
        f'text-transform:uppercase;color:{_MUTED};letter-spacing:.4px;">Device</th>'
        f'<th align="left" style="padding:8px 12px;font-size:11px;text-transform:uppercase;color:{_MUTED};">Issue</th>'
        f'<th align="right" style="padding:8px 12px;font-size:11px;text-transform:uppercase;color:{_MUTED};">Downtime</th>'
        f'<th align="right" style="padding:8px 12px;font-size:11px;text-transform:uppercase;color:{_MUTED};">Alerts</th></tr>'
    ]
    for it in issues[:8]:
        sev = _SEV.get((it.get("severity") or "info").lower(), _BRAND)
        rows.append(
            f'<tr style="border-top:1px solid {_BORDER};">'
            f'<td style="padding:9px 12px;font-size:13px;font-weight:600;color:{_INK};">{_esc(it.get("hostname","Unknown"))}</td>'
            f'<td style="padding:9px 12px;font-size:13px;">'
            f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;'
            f'font-weight:600;color:#fff;background:{sev};">{_esc((it.get("issue") or "").upper() or "—")}</span></td>'
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
        av_color = _GOOD if (av or 0) >= 99 else (_WARN if (av or 0) >= 95 else _BAD)
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


def _header_block(meta: dict, *, status_chip: str | None = None) -> str:
    product = _esc(meta.get("product_name", "ZenPlus"))
    company = _esc(meta.get("company_name") or "")
    title = _esc(meta.get("title") or report_title(meta.get("report_type", "")))
    plabel = _esc(meta.get("period_label") or period_label(meta.get("period", "")))
    gen = _fmt_ts(meta.get("generated_at"))
    chip = ""
    if status_chip:
        chip = (
            f'<span style="display:inline-block;background:{_BRAND};color:#fff;font-size:11px;'
            f'font-weight:700;letter-spacing:.6px;padding:5px 11px;border-radius:999px;">{_esc(status_chip)}</span>'
        )
    sub = f"{company} &middot; {plabel}" if company else plabel
    return (
        f'<tr><td style="height:6px;background:{_BRAND};font-size:0;line-height:0;">&nbsp;</td></tr>'
        f'<tr><td style="padding:22px 28px 4px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="font-size:16px;font-weight:700;color:{_INK};letter-spacing:.2px;">{product}</td>'
        f'<td align="right">{chip}</td></tr></table></td></tr>'
        f'<tr><td style="padding:8px 28px 2px;">'
        f'<div style="font-size:22px;line-height:1.25;font-weight:700;color:{_INK};">{title}</div>'
        f'<div style="margin-top:4px;font-size:13px;color:{_MUTED};">{sub} &middot; generated {_esc(gen)}</div>'
        f'</td></tr>'
    )


def _footer_block(meta: dict, *, email: bool) -> str:
    product = _esc(meta.get("product_name", "ZenPlus"))
    note = (
        "This report was delivered automatically because a notification channel "
        "is linked to a report schedule."
        if email else
        "Automatically generated report. Figures cover the period shown above."
    )
    return (
        f'<tr><td style="padding:18px 28px 26px;">'
        f'<div style="border-top:1px solid {_BORDER};padding-top:14px;color:{_MUTED};font-size:12px;line-height:1.5;">'
        f'{note}<br>&copy; {datetime.now(timezone.utc).year} {product}.</div>'
        f'</td></tr>'
    )


def _shell(inner: str, *, title: str, width: int = 720) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta name="x-apple-disable-message-reformatting">
<title>{_esc(title)}</title></head>
<body style="margin:0;padding:0;background:{_BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_BG};padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="{width}" cellpadding="0" cellspacing="0" style="max-width:{width}px;width:100%;background:{_CARD};border:1px solid {_BORDER};border-radius:14px;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
{inner}
</table>
</td></tr></table>
</body></html>"""


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

def build_report_html(meta: dict, data: dict) -> str:
    """Standalone full HTML report page (token share link target)."""
    inner = (
        _header_block(meta, status_chip="REPORT")
        + f'<tr><td style="padding:18px 22px 0;">{_kpi_grid_html(data)}</td></tr>'
        + f'<tr><td style="padding:0 28px;">{_top_issues_html(data)}</td></tr>'
        + f'<tr><td style="padding:0 28px;">{_locations_html(data)}</td></tr>'
        + _footer_block(meta, email=False)
    )
    return _shell(inner, title=meta.get("title") or report_title(meta.get("report_type", "")))


def build_report_email_html(meta: dict, data: dict, view_url: str | None = None,
                            attached: bool = False) -> str:
    """Email body: KPI summary + prominent 'View full report' button."""
    button = ""
    if view_url:
        button = (
            f'<tr><td style="padding:6px 28px 4px;">'
            f'<a href="{_esc(view_url)}" style="display:inline-block;background:{_BRAND};color:#ffffff;'
            f'text-decoration:none;font-size:14px;font-weight:600;padding:12px 24px;border-radius:8px;">'
            f'View full report &rarr;</a></td></tr>'
        )
    attach_note = ""
    if attached:
        attach_note = (
            f'<tr><td style="padding:4px 28px 0;">'
            f'<div style="font-size:12px;color:{_MUTED};">The full report is attached to this email.</div>'
            f'</td></tr>'
        )
    inner = (
        _header_block(meta, status_chip="SCHEDULED REPORT")
        + f'<tr><td style="padding:16px 22px 0;">{_kpi_grid_html(data)}</td></tr>'
        + f'<tr><td style="padding:6px 28px 0;">{_top_issues_html(data)}</td></tr>'
        + button
        + attach_note
        + _footer_block(meta, email=True)
    )
    return _shell(inner, title=meta.get("title") or report_title(meta.get("report_type", "")), width=640)


def build_report_email_text(meta: dict, data: dict, view_url: str | None = None) -> str:
    k = (data or {}).get("kpis", {}) or {}
    title = meta.get("title") or report_title(meta.get("report_type", ""))
    plabel = meta.get("period_label") or period_label(meta.get("period", ""))
    lines = [
        f"{title}",
        f"{(meta.get('company_name') or 'ZenPlus')} · {plabel}",
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
    if view_url:
        lines += ["", f"View full report: {view_url}"]
    lines += ["", f"— {meta.get('product_name', 'ZenPlus')} automated report"]
    return "\n".join(lines)
