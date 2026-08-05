"""Professional HTML email rendering — one visual system for every mail ZenPlus sends.

Three template families share a single branded shell (logo badge, status pill,
hero icon, details panel, footer) so alerts, operational notifications, account
security notices, and report deliveries all look like one product:

* ``build_alert_email_html/_text(ctx)``        — alert triggered / resolved
  (device & service status, host metrics, network-device and APM rules).
* ``build_notification_email_html/_text(ctx)`` — neutral notifications:
  SMTP/channel tests, system events, maintenance notes.
* ``build_account_email_html/_text(ctx)``      — account security notices:
  password reset / password changed.

Built with tables + inline CSS only — no flexbox/grid, no <style> blocks relied
upon — so rendering is consistent in Outlook, Gmail, Apple Mail, and mobile
clients. Every context dict is tolerant: missing keys degrade gracefully.

Alert ctx keys (all optional):
  severity      'critical' | 'warning' | 'info'
  resolved      bool — render as a green "RESOLVED" state
  status        short status label (e.g. 'DOWN', 'ALERT'); defaults from resolved
  title         headline (usually the rule name)
  rule_name     fallback for title
  hostname      device/host name
  ip_address    device IP
  message       the human-readable alert sentence
  details       list of (label, value) pairs rendered as a table
  timestamp     ISO string or display string
  action_url    optional "View in dashboard" button target
  product_name  defaults to 'ZenPlus'

Notification ctx adds nothing new; account ctx additionally understands:
  recipient_name  greeting name ("Hi Khuram,")
  changed_by      who performed the action (admin username or 'you')
  security_note   override for the "wasn't you?" line
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

# ── Palette (kept in sync with report_html.py and the dashboard) ─────────────
_ACCENT = {
    "critical": "#DC2626",
    "warning": "#F59E0B",
    "info": "#2563EB",
}
_TINT = {
    "critical": ("#FEF2F2", "#FECACA"),
    "warning": ("#FFFBEB", "#FDE68A"),
    "info": ("#EFF6FF", "#BFDBFE"),
}
_RESOLVED_COLOR = "#16A34A"
_RESOLVED_TINT = ("#F0FDF4", "#BBF7D0")
_BRAND = "#2563EB"
_INK = "#0F172A"
_MUTED = "#64748B"
_BORDER = "#E2E8F0"
_BG = "#F1F5F9"
_CARD = "#FFFFFF"
_PANEL = "#F8FAFC"

_FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
         "Arial,sans-serif")


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def severity_hex(severity: str, resolved: bool = False) -> str:
    if resolved:
        return _RESOLVED_COLOR
    return _ACCENT.get((severity or "warning").lower(), _ACCENT["warning"])


def _severity_tint(severity: str, resolved: bool = False) -> tuple[str, str]:
    if resolved:
        return _RESOLVED_TINT
    return _TINT.get((severity or "warning").lower(), _TINT["warning"])


def _status_label(ctx: dict) -> str:
    if ctx.get("resolved"):
        return "RESOLVED"
    return str(ctx.get("status") or "ALERT").upper()


def _fmt_ts(ts) -> str:
    if not ts:
        ts = datetime.now(timezone.utc).isoformat()
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return _esc(ts)
    try:
        return ts.strftime("%b %d, %Y · %H:%M %Z").strip() or ts.strftime("%b %d, %Y · %H:%M UTC")
    except Exception:
        return _esc(str(ts))


# ── Shared building blocks ───────────────────────────────────────────────────

def _shell(inner: str, *, title: str, preheader: str, accent: str) -> str:
    """Outer document: background, 600px card, accent bar, hidden preheader."""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta name="x-apple-disable-message-reformatting">
<title>{_esc(title)}</title></head>
<body style="margin:0;padding:0;background:{_BG};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{_esc(preheader)}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_BG};padding:28px 12px;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:{_CARD};border:1px solid {_BORDER};border-radius:14px;overflow:hidden;font-family:{_FONT};">
  <tr><td style="height:6px;background:{accent};font-size:0;line-height:0;">&nbsp;</td></tr>
{inner}
</table>
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
  <tr><td style="padding:14px 8px 0;text-align:center;font-family:{_FONT};font-size:11px;color:{_MUTED};">
    Automated message from your monitoring appliance &middot; please do not reply
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


def _header(product: str, *, chip_label: str, chip_color: str) -> str:
    """Logo badge + wordmark on the left, status pill on the right."""
    return f"""  <tr><td style="padding:20px 28px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td width="36" valign="middle">
        <div style="width:32px;height:32px;background:{_BRAND};border-radius:8px;color:#ffffff;font-size:17px;font-weight:800;text-align:center;line-height:32px;font-family:{_FONT};">Z</div>
      </td>
      <td valign="middle" style="padding-left:10px;">
        <div style="font-size:16px;font-weight:700;color:{_INK};letter-spacing:.2px;">{product}</div>
        <div style="font-size:10px;font-weight:600;color:{_MUTED};letter-spacing:.8px;text-transform:uppercase;">Network Monitoring</div>
      </td>
      <td align="right" valign="middle">
        <span style="display:inline-block;background:{chip_color};color:#ffffff;font-size:11px;font-weight:700;letter-spacing:.6px;padding:6px 13px;border-radius:999px;">{chip_label}</span>
      </td>
    </tr></table>
  </td></tr>"""


def _hero(title: str, subtitle: str, *, icon: str, accent: str) -> str:
    """Icon circle + headline + muted meta line."""
    return f"""  <tr><td style="padding:22px 28px 4px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td width="52" valign="top">
        <div style="width:44px;height:44px;background:{accent};border-radius:22px;color:#ffffff;font-size:21px;font-weight:800;text-align:center;line-height:44px;font-family:{_FONT};">{icon}</div>
      </td>
      <td valign="middle" style="padding-left:14px;">
        <div style="font-size:20px;line-height:1.3;font-weight:700;color:{_INK};">{title}</div>
        <div style="margin-top:3px;font-size:13px;color:{_MUTED};">{subtitle}</div>
      </td>
    </tr></table>
  </td></tr>"""


def _callout(message: str, *, accent: str, tint: str) -> str:
    if not message:
        return ""
    return f"""  <tr><td style="padding:16px 28px 0;">
    <div style="background:{tint};border-left:4px solid {accent};border-radius:6px;padding:14px 16px;color:{_INK};font-size:15px;line-height:1.55;">{message}</div>
  </td></tr>"""


def _details_panel(pairs: list, *, raw_labels: frozenset = frozenset()) -> str:
    """Rounded soft panel with LABEL / value rows. Values in ``raw_labels`` rows
    are trusted pre-escaped HTML; everything else is escaped here."""
    rows = []
    for i, (label, value) in enumerate(pairs):
        if value in (None, ""):
            continue
        sep = f"border-top:1px solid {_BORDER};" if rows else ""
        val = value if label in raw_labels else _esc(value)
        rows.append(
            f'<tr>'
            f'<td style="padding:9px 16px;{sep}color:{_MUTED};font-size:11px;font-weight:600;'
            f'letter-spacing:.5px;text-transform:uppercase;width:140px;vertical-align:top;">{_esc(label)}</td>'
            f'<td style="padding:9px 16px;{sep}color:{_INK};font-size:13px;font-weight:600;'
            f'vertical-align:top;">{val}</td>'
            f'</tr>'
        )
    if not rows:
        return ""
    return f"""  <tr><td style="padding:16px 28px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_PANEL};border:1px solid {_BORDER};border-radius:10px;border-collapse:separate;overflow:hidden;">
      {"".join(rows)}
    </table>
  </td></tr>"""


def _button(url: str, label: str, *, color: str) -> str:
    if not url:
        return ""
    return f"""  <tr><td style="padding:20px 28px 0;">
    <a href="{_esc(url)}" style="display:inline-block;background:{color};color:#ffffff;text-decoration:none;font-size:14px;font-weight:600;padding:12px 26px;border-radius:8px;">{_esc(label)} &rarr;</a>
  </td></tr>"""


def _footer(product: str, reason: str) -> str:
    year = datetime.now(timezone.utc).year
    return f"""  <tr><td style="padding:22px 28px 24px;">
    <div style="border-top:1px solid {_BORDER};padding-top:14px;color:{_MUTED};font-size:12px;line-height:1.6;">
      {reason}<br>&copy; {year} {product} &middot; This mailbox is not monitored.
    </div>
  </td></tr>"""


# ── Alert emails ─────────────────────────────────────────────────────────────

def build_alert_email_text(ctx: dict) -> str:
    """Plain-text alternative (always attached alongside the HTML)."""
    sev = (ctx.get("severity") or "warning").upper()
    lines = [
        f"[{_status_label(ctx)}] {ctx.get('title') or ctx.get('rule_name') or 'Alert'}",
        "",
    ]
    if ctx.get("message"):
        lines += [str(ctx["message"]), ""]
    if ctx.get("hostname"):
        host = str(ctx["hostname"])
        if ctx.get("ip_address"):
            host += f" ({ctx['ip_address']})"
        lines.append(f"Host:      {host}")
    lines.append(f"Severity:  {sev}")
    for label, value in (ctx.get("details") or []):
        if value not in (None, ""):
            lines.append(f"{label + ':':<11}{value}")
    lines.append(f"Time:      {_fmt_ts(ctx.get('timestamp'))}")
    if ctx.get("action_url"):
        lines += ["", f"View in dashboard: {ctx['action_url']}"]
    lines += ["", f"— {ctx.get('product_name', 'ZenPlus')} automated alert"]
    return "\n".join(lines)


def build_alert_email_html(ctx: dict) -> str:
    product = _esc(ctx.get("product_name", "ZenPlus"))
    resolved = bool(ctx.get("resolved"))
    severity = (ctx.get("severity") or "warning").lower()
    accent = severity_hex(severity, resolved)
    tint, _tint_border = _severity_tint(severity, resolved)
    status = _esc(_status_label(ctx))
    sev_label = _esc(severity.upper())
    title = _esc(ctx.get("title") or ctx.get("rule_name") or "Alert")
    message = _esc(ctx.get("message") or "")
    hostname = _esc(ctx.get("hostname") or "")
    ip_address = _esc(ctx.get("ip_address") or "")
    ts = _fmt_ts(ctx.get("timestamp"))
    icon = "&#10003;" if resolved else ("!" if severity in ("critical", "warning") else "i")
    preheader = (ctx.get("message") or f"{_status_label(ctx)} — {ctx.get('title') or 'Alert'}")[:140]

    subtitle_bits = [f'<span style="color:{accent};font-weight:700;">{sev_label}</span>']
    if hostname:
        subtitle_bits.append(hostname)
    subtitle_bits.append(_esc(ts))
    subtitle = " &middot; ".join(subtitle_bits)

    detail_pairs = []
    if hostname:
        host_val = hostname + (f" &middot; {ip_address}" if ip_address else "")
        detail_pairs.append(("Host", host_val))
    detail_pairs += list(ctx.get("details") or [])
    detail_pairs.append(("Severity", sev_label))
    detail_pairs.append(("Time", ts))

    inner = (
        _header(product, chip_label=status, chip_color=accent)
        + _hero(title, subtitle, icon=icon, accent=accent)
        + _callout(message, accent=accent, tint=tint)
        + _details_panel(detail_pairs, raw_labels=frozenset({"Host"}))
        + _button(ctx.get("action_url") or "", f"View in {ctx.get('product_name', 'ZenPlus')}", color=accent)
        + _footer(product,
                  "This is an automated alert from " + product + ". You received it because "
                  "a notification channel is linked to the triggering alert rule.")
    )
    return _shell(inner, title=ctx.get("title") or ctx.get("rule_name") or "Alert",
                  preheader=preheader, accent=accent)


# ── Operational notifications (tests, system events, maintenance) ────────────

def build_notification_email_text(ctx: dict) -> str:
    lines = [ctx.get("title") or "Notification", ""]
    if ctx.get("message"):
        lines += [str(ctx["message"]), ""]
    for label, value in (ctx.get("details") or []):
        if value not in (None, ""):
            lines.append(f"{label + ':':<14}{value}")
    lines.append(f"{'Time:':<14}{_fmt_ts(ctx.get('timestamp'))}")
    if ctx.get("action_url"):
        lines += ["", f"Open dashboard: {ctx['action_url']}"]
    lines += ["", f"— {ctx.get('product_name', 'ZenPlus')}"]
    return "\n".join(lines)


def build_notification_email_html(ctx: dict) -> str:
    product = _esc(ctx.get("product_name", "ZenPlus"))
    title = _esc(ctx.get("title") or "Notification")
    message = _esc(ctx.get("message") or "")
    ts = _fmt_ts(ctx.get("timestamp"))
    status = _esc((ctx.get("status") or "NOTICE").upper())
    tint, _b = _TINT["info"]
    preheader = (ctx.get("message") or ctx.get("title") or "Notification")[:140]

    detail_pairs = list(ctx.get("details") or [])
    detail_pairs.append(("Time", ts))

    inner = (
        _header(product, chip_label=status, chip_color=_BRAND)
        + _hero(title, f"{product} system notification &middot; {_esc(ts)}", icon="i", accent=_BRAND)
        + _callout(message, accent=_BRAND, tint=tint)
        + _details_panel(detail_pairs)
        + _button(ctx.get("action_url") or "", f"Open {ctx.get('product_name', 'ZenPlus')}", color=_BRAND)
        + _footer(product, "This is an operational notification from " + product + ".")
    )
    return _shell(inner, title=ctx.get("title") or "Notification",
                  preheader=preheader, accent=_BRAND)


# ── Account security notices (password reset / changed) ──────────────────────

def build_account_email_text(ctx: dict) -> str:
    name = ctx.get("recipient_name") or ""
    lines = [ctx.get("title") or "Account update", ""]
    if name:
        lines += [f"Hi {name},", ""]
    if ctx.get("message"):
        lines += [str(ctx["message"]), ""]
    for label, value in (ctx.get("details") or []):
        if value not in (None, ""):
            lines.append(f"{label + ':':<14}{value}")
    lines.append(f"{'Time:':<14}{_fmt_ts(ctx.get('timestamp'))}")
    note = ctx.get("security_note") or (
        "If you did not expect this change, contact your administrator immediately."
    )
    lines += ["", note]
    if ctx.get("action_url"):
        lines += ["", f"Sign in: {ctx['action_url']}"]
    lines += ["", f"— {ctx.get('product_name', 'ZenPlus')}"]
    return "\n".join(lines)


def build_account_email_html(ctx: dict) -> str:
    product = _esc(ctx.get("product_name", "ZenPlus"))
    title = _esc(ctx.get("title") or "Account update")
    name = _esc(ctx.get("recipient_name") or "")
    message = _esc(ctx.get("message") or "")
    ts = _fmt_ts(ctx.get("timestamp"))
    tint, tint_border = _TINT["info"]
    preheader = (ctx.get("message") or ctx.get("title") or "Account update")[:140]

    greeting = ""
    if name:
        greeting = (f'  <tr><td style="padding:16px 28px 0;">'
                    f'<div style="font-size:15px;color:{_INK};">Hi {name},</div></td></tr>')

    detail_pairs = list(ctx.get("details") or [])
    detail_pairs.append(("Time", ts))

    note = _esc(ctx.get("security_note") or
                "If you did not expect this change, contact your administrator immediately.")
    security = (
        f'  <tr><td style="padding:16px 28px 0;">'
        f'<div style="background:{tint};border:1px solid {tint_border};border-radius:10px;'
        f'padding:12px 16px;color:{_INK};font-size:13px;line-height:1.55;">'
        f'<span style="font-weight:700;">Security note:</span> {note}</div></td></tr>'
    )

    inner = (
        _header(product, chip_label="ACCOUNT", chip_color=_BRAND)
        + _hero(title, f"Account security notice &middot; {_esc(ts)}", icon="&#10003;", accent=_BRAND)
        + greeting
        + _callout(message, accent=_BRAND, tint=_PANEL)
        + _details_panel(detail_pairs)
        + security
        + _button(ctx.get("action_url") or "", f"Sign in to {ctx.get('product_name', 'ZenPlus')}", color=_BRAND)
        + _footer(product, "This security notice was sent to the email address on your "
                           + product + " account.")
    )
    return _shell(inner, title=ctx.get("title") or "Account update",
                  preheader=preheader, accent=_BRAND)
