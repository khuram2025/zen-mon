"""Professional HTML alert email rendering.

A single, email-client-safe HTML template used by every notification path
(device/service status changes in alert_engine, host-metric alerts, and
network-device alerts). Built with tables + inline CSS only — no flexbox/grid,
no <style> media queries relied upon — so it renders consistently in Outlook,
Gmail, Apple Mail, and mobile clients.

Call ``build_alert_email_html(ctx)`` / ``build_alert_email_text(ctx)`` with a
tolerant context dict; missing keys degrade gracefully.

ctx keys (all optional):
  severity      'critical' | 'warning' | 'info'
  resolved      bool — render as a green "RESOLVED" banner
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
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

_ACCENT = {
    "critical": "#DC2626",
    "warning": "#F59E0B",
    "info": "#2563EB",
}
_RESOLVED_COLOR = "#16A34A"
_INK = "#0F172A"
_MUTED = "#64748B"
_BORDER = "#E2E8F0"
_BG = "#F1F5F9"
_CARD = "#FFFFFF"


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def severity_hex(severity: str, resolved: bool = False) -> str:
    if resolved:
        return _RESOLVED_COLOR
    return _ACCENT.get((severity or "warning").lower(), _ACCENT["warning"])


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
    accent = severity_hex(ctx.get("severity", "warning"), resolved)
    status = _esc(_status_label(ctx))
    sev_label = _esc((ctx.get("severity") or "warning").upper())
    title = _esc(ctx.get("title") or ctx.get("rule_name") or "Alert")
    message = _esc(ctx.get("message") or "")
    hostname = _esc(ctx.get("hostname") or "")
    ip_address = _esc(ctx.get("ip_address") or "")
    ts = _fmt_ts(ctx.get("timestamp"))
    preheader = _esc((ctx.get("message") or f"{status} — {title}")[:140])

    # Details rows
    rows = []
    detail_pairs = list(ctx.get("details") or [])
    if hostname:
        host_val = hostname + (f" &middot; {ip_address}" if ip_address else "")
        detail_pairs = [("Host", host_val)] + detail_pairs
    detail_pairs.append(("Severity", sev_label))
    detail_pairs.append(("Time", _esc(ts)))
    for label, value in detail_pairs:
        if value in (None, ""):
            continue
        rows.append(
            f'<tr>'
            f'<td style="padding:7px 0;color:{_MUTED};font-size:13px;width:130px;'
            f'vertical-align:top;">{_esc(label)}</td>'
            f'<td style="padding:7px 0;color:{_INK};font-size:13px;font-weight:600;'
            f'vertical-align:top;">{value if label=="Host" else _esc(value)}</td>'
            f'</tr>'
        )
    rows_html = "".join(rows)

    action_html = ""
    if ctx.get("action_url"):
        url = _esc(ctx["action_url"])
        action_html = (
            f'<tr><td style="padding:8px 0 4px;">'
            f'<a href="{url}" style="display:inline-block;background:{accent};color:#ffffff;'
            f'text-decoration:none;font-size:14px;font-weight:600;padding:11px 22px;'
            f'border-radius:8px;">View in {product} &rarr;</a></td></tr>'
        )

    message_html = ""
    if message:
        message_html = (
            f'<tr><td style="padding:0 0 18px;">'
            f'<div style="background:{_BG};border-left:4px solid {accent};border-radius:6px;'
            f'padding:14px 16px;color:{_INK};font-size:15px;line-height:1.5;">{message}</div>'
            f'</td></tr>'
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta name="x-apple-disable-message-reformatting">
<title>{title}</title></head>
<body style="margin:0;padding:0;background:{_BG};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_BG};padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:{_CARD};border:1px solid {_BORDER};border-radius:14px;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">

  <!-- accent bar -->
  <tr><td style="height:6px;background:{accent};font-size:0;line-height:0;">&nbsp;</td></tr>

  <!-- header -->
  <tr><td style="padding:22px 28px 6px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="font-size:16px;font-weight:700;color:{_INK};letter-spacing:.2px;">{product}</td>
      <td align="right">
        <span style="display:inline-block;background:{accent};color:#ffffff;font-size:11px;
        font-weight:700;letter-spacing:.6px;padding:5px 11px;border-radius:999px;">{status}</span>
      </td>
    </tr></table>
  </td></tr>

  <!-- title -->
  <tr><td style="padding:10px 28px 6px;">
    <div style="font-size:20px;line-height:1.3;font-weight:700;color:{_INK};">{title}</div>
  </td></tr>

  <!-- body -->
  <tr><td style="padding:14px 28px 4px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      {message_html}
      <tr><td style="padding:2px 0 4px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="border-top:1px solid {_BORDER};">
          {rows_html}
        </table>
      </td></tr>
      {action_html}
    </table>
  </td></tr>

  <!-- footer -->
  <tr><td style="padding:18px 28px 24px;">
    <div style="border-top:1px solid {_BORDER};padding-top:14px;color:{_MUTED};font-size:12px;line-height:1.5;">
      This is an automated alert from {product}. You received it because a notification
      channel is linked to the triggering alert rule.
    </div>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""
