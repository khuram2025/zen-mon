"""
Alert evaluation engine - called by the Go poller on status changes.
Evaluates matching alert rules and sends notifications.
"""
import json
import httpx
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from uuid import UUID
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.database import get_db
from app.services.email_render import build_alert_email_html
from app.services.tag_service import tag_set as _tag_set
from app.services import alert_notify_state as ns
from app.services.alert_schedule import notifications_allowed, get_configured_timezone
from app.services.alert_phrasing import (
    DEFAULT_EMAIL_BODY, DEFAULT_EMAIL_SUBJECT, DEFAULT_RECOVERY_EMAIL_BODY,
    DEFAULT_RECOVERY_EMAIL_SUBJECT, DEFAULT_RECOVERY_SMS, DEFAULT_SMS,
    conditions_label, duration_between, effective_template, rule_phrasing,
)
from pydantic import BaseModel

router = APIRouter(prefix="/alert-engine", tags=["Alert Engine (Internal)"])


class StatusChangeEvent(BaseModel):
    device_id: str
    hostname: str
    ip_address: str
    old_status: str
    new_status: str
    device_type: Optional[str] = None
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    location: Optional[str] = None
    rtt_ms: float = 0
    packet_loss: float = 0


class TrapEvent(BaseModel):
    device_id: Optional[str] = None
    source_ip: str
    trap_oid: str
    trap_name: Optional[str] = None
    severity: str = "info"
    message: Optional[str] = None


class ServiceStatusChangeEvent(BaseModel):
    service_check_id: str
    check_name: str
    check_type: str
    old_status: str
    new_status: str
    device_id: Optional[str] = None
    device_hostname: Optional[str] = None
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    tags: list[str] = []
    response_ms: float = 0
    error: Optional[str] = None
    target: Optional[str] = None


async def _dashboard_url(db: AsyncSession, path: str = "/alerts") -> str:
    """Absolute link back into the dashboard, or "" when it cannot be known.

    Alert notifications run in background loops with no request to read a Host
    header from, so this uses the same order as the agent installer: APP_BASE_URL
    from .env, then the company `base_url` system setting. Returning "" simply
    omits the button rather than emailing a link that goes nowhere.
    """
    from app.core.config import get_settings
    base = (get_settings().APP_BASE_URL or "").strip()
    if not base:
        try:
            row = (await db.execute(
                text("SELECT value->>'base_url' FROM system_settings WHERE key = 'company'")
            )).first()
            base = (row[0] or "").strip() if row else ""
        except Exception:
            base = ""
    if not base:
        return ""
    return base.rstrip("/") + path


async def _smtp_gateway(db: AsyncSession, gw_id) -> Optional[dict]:
    """Resolve the SMTP gateway for a channel, honouring the `enabled` column.

    `enabled` is a column on notification_gateways — the Gateways page toggles
    it and never touches config.enabled — so reading the JSON blob meant a
    gateway disabled in the UI kept sending alerts.
    """
    if gw_id:
        row = (await db.execute(
            text("SELECT config, enabled FROM notification_gateways WHERE id = :id"),
            {"id": gw_id})).first()
    else:
        row = (await db.execute(text(
            "SELECT config, enabled FROM notification_gateways "
            "WHERE type = 'smtp' AND is_default = true LIMIT 1"))).first()
    if not row or not row.enabled:
        return None
    return dict(row.config)


def _clean_details(pairs: list) -> list:
    """Drop rows the alert had no value for.

    The details panel was previously fed Group/Location/Device type verbatim;
    on most appliances those are unset, so the panel rendered nearly empty and
    the mail looked like a bare paragraph.
    """
    out = []
    for label, value in pairs:
        if value is None:
            continue
        text_value = str(value).strip()
        if not text_value or text_value.lower() in ("none", "n/a", "unknown"):
            continue
        out.append((label, text_value))
    return out


def _render(template: str, variables: dict) -> str:
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


async def _send_sms(gw_config: dict, phones: str, message: str):
    """Send SMS via custom HTTP gateway."""
    if gw_config.get("provider") != "custom_http" or not gw_config.get("api_url"):
        return

    template = gw_config.get("request_template", "")
    template = template.replace("{recipients}", phones)
    template = template.replace("{message}", message)
    template = template.replace("{sender}", gw_config.get("sender_name", "ZenPlus"))

    headers = dict(gw_config.get("custom_headers", {}))
    auth = None
    if gw_config.get("auth_type") == "basic":
        auth = (gw_config.get("auth_username", ""), gw_config.get("auth_password", ""))

    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
        if gw_config.get("http_method", "GET").upper() == "POST":
            await client.post(gw_config["api_url"], content=template, headers=headers, auth=auth)
        else:
            url = gw_config["api_url"]
            if template:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}{template}"
            await client.get(url, headers=headers, auth=auth)


async def _send_email(gw_config: dict, recipients: str, subject: str, body: str,
                      html_body: str | None = None):
    """Send email via SMTP gateway.

    When ``html_body`` is provided the message is sent as multipart/alternative
    (plain ``body`` + HTML), so clients render the professional HTML template
    while text-only clients still get a readable fallback.
    """
    if not gw_config.get("host"):
        return

    recipient_list = [r.strip() for r in recipients.split(",") if r.strip()]
    if html_body:
        msg = MIMEMultipart("alternative")
    else:
        msg = MIMEMultipart()
    msg["From"] = f"{gw_config.get('from_name', 'ZenPlus')} <{gw_config.get('from_email', '')}>"
    msg["To"] = ", ".join(recipient_list)
    msg["Subject"] = subject
    # For multipart/alternative the LAST part is the client's preferred form, so
    # attach plain first, HTML second.
    msg.attach(MIMEText(body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))

    enc = gw_config.get("encryption", "tls")
    if enc == "ssl":
        server = smtplib.SMTP_SSL(gw_config["host"], gw_config.get("port", 465), timeout=10)
    else:
        server = smtplib.SMTP(gw_config["host"], gw_config.get("port", 587), timeout=10)
        if enc == "tls":
            server.starttls()
    if gw_config.get("username"):
        server.login(gw_config["username"], gw_config.get("password", ""))
    server.sendmail(gw_config.get("from_email", ""), recipient_list, msg.as_string())
    server.quit()


def _severity_color(severity: str) -> int:
    s = (severity or "warning").lower()
    return 0xDC2626 if s == "critical" else 0xF59E0B if s == "warning" else 0x2563EB


async def _send_webhook(ch_config: dict, ctx: dict) -> None:
    """Generic JSON webhook. Posts the alert ctx + optional templated body.

    `ctx` carries: hostname, ip_address, status (UP/DOWN/DEGRADED), severity,
    message, triggered_at, map_id (optional), rule_id, is_recovery.
    """
    url = ch_config.get("url") or ch_config.get("webhook_url")
    if not url:
        return
    headers = {"Content-Type": "application/json"}
    headers.update(dict(ch_config.get("headers") or {}))
    if ch_config.get("auth_bearer"):
        headers["Authorization"] = f"Bearer {ch_config['auth_bearer']}"
    body = ch_config.get("body_template")
    payload: dict | str = ctx
    if isinstance(body, str) and body.strip():
        payload = _render(body, ctx)
    async with httpx.AsyncClient(timeout=10.0, verify=ch_config.get("tls_verify", True)) as client:
        if isinstance(payload, str):
            await client.post(url, content=payload, headers=headers)
        else:
            await client.post(url, json=payload, headers=headers)


async def _send_slack(ch_config: dict, ctx: dict) -> None:
    """Slack incoming-webhook compatible payload."""
    url = ch_config.get("url") or ch_config.get("webhook_url")
    if not url:
        return
    is_recovery = bool(ctx.get("is_recovery"))
    emoji = ":large_green_circle:" if is_recovery else (":red_circle:" if ctx.get("severity") == "critical" else ":warning:")
    title = f"{emoji} {ctx['hostname']} is {ctx['status']}"
    payload = {
        "text": title,
        "attachments": [{
            "color": "good" if is_recovery else ("danger" if ctx.get("severity") == "critical" else "warning"),
            "title": title,
            "fields": [
                {"title": "Host",       "value": ctx["hostname"],            "short": True},
                {"title": "IP",         "value": ctx.get("ip_address", "—"), "short": True},
                {"title": "Severity",   "value": ctx.get("severity", "—"),   "short": True},
                {"title": "Triggered",  "value": ctx.get("triggered_at", "—"), "short": True},
                {"title": "Message",    "value": ctx.get("message", ""),     "short": False},
            ],
            "footer": "ZenPlus",
            "ts": int(datetime.now(timezone.utc).timestamp()),
        }],
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(url, json=payload)


async def _send_teams(ch_config: dict, ctx: dict) -> None:
    """Microsoft Teams incoming-webhook Adaptive Card."""
    url = ch_config.get("url") or ch_config.get("webhook_url")
    if not url:
        return
    is_recovery = bool(ctx.get("is_recovery"))
    severity = ctx.get("severity", "warning")
    color = "good" if is_recovery else ("attention" if severity == "critical" else "warning")
    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "size": "Large", "weight": "Bolder",
                     "text": f"{ctx['hostname']} is {ctx['status']}",
                     "color": color},
                    {"type": "FactSet", "facts": [
                        {"title": "IP",        "value": ctx.get("ip_address", "—")},
                        {"title": "Severity",  "value": severity},
                        {"title": "Triggered", "value": ctx.get("triggered_at", "—")},
                        {"title": "Message",   "value": ctx.get("message", "")},
                    ]},
                ],
            },
        }],
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(url, json=card)


async def _send_discord(ch_config: dict, ctx: dict) -> None:
    """Discord-compatible webhook with an embed."""
    url = ch_config.get("url") or ch_config.get("webhook_url")
    if not url:
        return
    is_recovery = bool(ctx.get("is_recovery"))
    color = 0x16A34A if is_recovery else _severity_color(ctx.get("severity", "warning"))
    payload = {
        "username": ch_config.get("username", "ZenPlus"),
        "embeds": [{
            "title":  f"{ctx['hostname']} is {ctx['status']}",
            "color":  color,
            "fields": [
                {"name": "IP",        "value": ctx.get("ip_address", "—"), "inline": True},
                {"name": "Severity",  "value": ctx.get("severity", "—"),   "inline": True},
                {"name": "Triggered", "value": ctx.get("triggered_at", "—"), "inline": False},
                {"name": "Message",   "value": ctx.get("message", "")[:1000] or "—", "inline": False},
            ],
            "timestamp": ctx.get("triggered_at"),
            "footer": {"text": "ZenPlus"},
        }],
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(url, json=payload)


async def _send_pagerduty(ch_config: dict, ctx: dict) -> None:
    """PagerDuty Events API v2 — trigger / resolve based on is_recovery."""
    routing_key = ch_config.get("routing_key") or ch_config.get("integration_key")
    if not routing_key:
        return
    is_recovery = bool(ctx.get("is_recovery"))
    dedup_key = f"zenplus:{ctx.get('rule_id', 'unknown')}:{ctx.get('device_id', 'unknown')}"
    payload = {
        "routing_key": routing_key,
        "event_action": "resolve" if is_recovery else "trigger",
        "dedup_key": dedup_key,
        "payload": {
            "summary":   f"{ctx['hostname']} is {ctx['status']} — {ctx.get('message', '')}".strip(),
            "source":    ctx.get("ip_address") or ctx.get("hostname"),
            "severity":  {"critical": "critical", "warning": "warning", "info": "info"}.get(
                ctx.get("severity", "warning"), "warning",
            ),
            "custom_details": {k: v for k, v in ctx.items() if k not in ("severity",)},
        },
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post("https://events.pagerduty.com/v2/enqueue", json=payload)


async def _dispatch_channel(ch_type: str, ch_config: dict, ctx: dict) -> bool:
    """Single dispatch point. Returns True if a notification was attempted."""
    try:
        if ch_type == "webhook":   await _send_webhook(ch_config, ctx);   return True
        if ch_type == "slack":     await _send_slack(ch_config, ctx);     return True
        if ch_type == "teams":     await _send_teams(ch_config, ctx);     return True
        if ch_type == "discord":   await _send_discord(ch_config, ctx);   return True
        if ch_type == "pagerduty": await _send_pagerduty(ch_config, ctx); return True
    except Exception as exc:
        print(f"ERROR dispatch {ch_type}: {exc}")
    return False


async def _find_suppressing_dependency(db: AsyncSession, device_id: str):
    """Return the first unhealthy upstream dependency that should suppress alerts."""
    result = await db.execute(
        text("""
            SELECT td.id AS dependency_id,
                   td.parent_device_id,
                   p.hostname AS parent_hostname,
                   p.status AS parent_status,
                   td.dependency_type
            FROM topology_dependencies td
            JOIN devices p ON p.id = td.parent_device_id
            WHERE td.child_device_id = :device_id
              AND td.enabled = true
              AND td.suppress_alerts = true
              AND p.status IN ('down', 'degraded', 'unknown')
            ORDER BY
              CASE p.status WHEN 'down' THEN 1 WHEN 'degraded' THEN 2 ELSE 3 END,
              td.updated_at DESC
            LIMIT 1
        """),
        {"device_id": device_id},
    )
    return result.mappings().first()


def _cmp(value: float, operator: str, threshold: float) -> bool:
    """Compare a metric value against a threshold using the rule operator."""
    op = (operator or "").strip()
    if op in (">", "gt"):
        return value > threshold
    if op in (">=", "gte"):
        return value >= threshold
    if op in ("<", "lt"):
        return value < threshold
    if op in ("<=", "lte"):
        return value <= threshold
    if op in ("==", "eq"):
        return value == threshold
    if op in ("!=", "neq"):
        return value != threshold
    return False


def _eval_one(metric: str, operator: str, threshold, values: dict) -> bool:
    v = values.get(metric)
    if v is None or threshold is None:
        return False
    try:
        return _cmp(float(v), operator, float(threshold))
    except (TypeError, ValueError):
        return False


def _recovery_map(closed_rows) -> dict:
    """{rule_id: {"notified": bool, "since": datetime}} for the alert rows a
    recovery just resolved.

    Reset actions are gated on membership in this map: a reset only means
    something for a rule whose own trigger this recovery closes. Without that,
    every 'any'-scoped rule on the appliance — including metric rules that
    belong to the periodic evaluators and never fired — ran its reset actions
    on every device up-transition.

    ``notified`` is True when any of the rule's closed rows had its trigger
    dispatched; an absent flag predates tracking and counts as dispatched, so
    upgrading never swallows the all-clear for an incident already in flight.
    ``since`` is the oldest trigger time, which is what "active for" means.
    """
    out: dict = {}
    for row in closed_rows:
        if row.rule_id is None:
            continue
        rid = str(row.rule_id)
        sent = row.notified is None or str(row.notified).lower() == "true"
        entry = out.get(rid)
        if entry is None:
            out[rid] = {"notified": sent, "since": row.triggered_at}
            continue
        entry["notified"] = entry["notified"] or sent
        if row.triggered_at and (entry["since"] is None or row.triggered_at < entry["since"]):
            entry["since"] = row.triggered_at
    return out


def _conditions_match(rule, values: dict) -> bool:
    """
    E1: evaluate a rule's metric condition(s) against live metric values.

    - A non-empty ``conditions`` array is evaluated element-wise and combined by
      ``condition_logic`` (AND/OR).
    - Otherwise the legacy flat metric/operator/threshold is the single
      condition. Pure status rules (metric is ping_status/service_status, or
      absent) are NOT metric-gated and always return True, preserving the
      existing status-transition behaviour.
    """
    conds = getattr(rule, "conditions", None)
    if conds:
        results = [
            _eval_one(c.get("metric"), c.get("operator"), c.get("threshold"), values)
            for c in conds
        ]
        logic = (getattr(rule, "condition_logic", "AND") or "AND").upper()
        return any(results) if logic == "OR" else all(results)
    metric = getattr(rule, "metric", None)
    if metric in ("ping_status", "service_status", None):
        return True
    return _eval_one(metric, getattr(rule, "operator", None), getattr(rule, "threshold", None), values)


async def _device_in_maintenance(db: AsyncSession, device_id: str) -> bool:
    """True when the device is inside an active device_maintenance window.

    Defense in depth: the poller already suppresses transitions for devices in
    maintenance, but this also covers events raised while a window was being
    created, and traps. Tolerates the table not existing yet (rolling upgrade
    before migrate-053 has run).
    """
    try:
        row = await db.execute(
            text("""
                SELECT 1
                FROM device_maintenance m
                JOIN devices d ON d.id = :did AND (
                       (m.scope_type = 'device' AND m.scope_device_id = d.id)
                    OR (m.scope_type = 'group'  AND m.scope_group_id = d.group_id)
                    OR (m.scope_type = 'tag'    AND jsonb_exists(COALESCE(d.tags, '[]'::jsonb), m.scope_tag))
                    OR (m.scope_type = 'all')
                )
                WHERE m.starts_at <= now() AND m.ends_at >= now()
                LIMIT 1
            """),
            {"did": device_id},
        )
        return row.first() is not None
    except Exception:
        return False


@router.post("/evaluate")
async def evaluate_status_change(
    event: StatusChangeEvent,
    db: AsyncSession = Depends(get_db),
):
    """
    Called by the Go poller when a device status changes.
    Evaluates all matching alert rules and sends notifications.
    No auth required - internal endpoint.
    """
    now = datetime.now(timezone.utc)
    is_recovery = event.new_status == "up" and event.old_status in ("down", "degraded")
    is_down = event.new_status in ("down", "degraded")

    # Get device info for group/location/type matching
    dev_result = await db.execute(
        text("SELECT device_type, group_id, location, tags FROM devices WHERE id = :id"),
        {"id": event.device_id},
    )
    dev_row = dev_result.first()
    device_type = dev_row.device_type if dev_row else event.device_type
    group_id = str(dev_row.group_id) if dev_row and dev_row.group_id else event.group_id
    location = dev_row.location if dev_row else event.location
    device_tags = _tag_set(dev_row.tags if dev_row else None)

    # Planned downtime: never raise alerts for a device in an active
    # maintenance window. Recovery events still pass so open alerts resolve.
    if not is_recovery and await _device_in_maintenance(db, event.device_id):
        return {"evaluated": 0, "matched": 0, "suppressed": "maintenance"}

    # Get group name
    group_name = ""
    if group_id:
        gr = await db.execute(text("SELECT name FROM device_groups WHERE id = :id"), {"id": group_id})
        gr_row = gr.first()
        group_name = gr_row.name if gr_row else ""

    # Fetch all enabled alert rules. Service-check rules are excluded: their
    # metric ('service_status') is treated as always-matching by the condition
    # gate, so a device ping transition used to fire them — "HTTP health:
    # SWITCH is DEGRADED" alerts about a check nobody ran.
    rules_result = await db.execute(
        text("""
            SELECT id, name, trigger_on, recovery_alert, severity,
                   device_id, group_id, device_type, location, scope_tag,
                   notify_channels, cooldown,
                   metric, operator, threshold, conditions, condition_logic,
                   email_subject, email_body, sms_template,
                   recovery_email_subject, recovery_email_body, recovery_sms_template,
                   schedule_start, schedule_end, schedule_days
            FROM alert_rules
            WHERE enabled = true
              AND service_check_id IS NULL
              AND service_check_group_id IS NULL
              AND COALESCE(metric, '') <> 'service_status'
        """)
    )
    rules = rules_result.fetchall()

    # Template variables
    variables = {
        "hostname": event.hostname,
        "ip_address": event.ip_address,
        "status": event.new_status.upper(),
        "severity": "",
        "rule_name": "",
        "group": group_name,
        "location": location or "",
        "device_type": device_type or "",
        "metric": "ping_status",
        "operator": "==",
        "threshold": "0",
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "duration": "",
        "duration_sentence": "",
        "duration_suffix": "",
        "rtt": f"{event.rtt_ms:.1f}ms",
        "packet_loss": f"{event.packet_loss:.0%}",
        "status_intro": "The following alert has been resolved:" if is_recovery else "An alert has been triggered:",
    }

    notifications_sent = 0
    resolved_alerts = 0
    suppressed_alerts = 0
    _tz = await get_configured_timezone(db)
    suppressing_dependency = await _find_suppressing_dependency(db, event.device_id) if is_down else None

    # Recovery closes ALL open down/degraded status alerts on this device,
    # before any per-rule gating. This used to live inside the rules loop
    # behind `rule.recovery_alert`, so rules without a recovery notification
    # never resolved their alerts — one firewall accumulated 100+ permanently
    # "active" DEGRADED alerts, poisoning every alert counter in the UI.
    # Service-check alerts are deliberately NOT closed here: the device
    # answering ping again says nothing about its HTTP check passing.
    recovered: dict[str, dict] = {}
    outage_duration = ""
    if is_recovery:
        closed = (await db.execute(
            text("""
                UPDATE alerts
                SET status = 'resolved',
                    resolved_at = :resolved_at,
                    metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:resolution_metadata AS jsonb)
                WHERE status IN ('active', 'acknowledged')
                  AND device_id = :device_id
                  AND service_check_id IS NULL
                  AND COALESCE(metadata->>'is_recovery', 'false') != 'true'
                  AND COALESCE(metadata->>'new_status', '') IN ('down', 'degraded')
                RETURNING rule_id, triggered_at, metadata->>'notified' AS notified
            """),
            {
                "resolved_at": now,
                "device_id": event.device_id,
                "resolution_metadata": json.dumps({
                    "resolved_by_recovery": True,
                    "recovery_status": event.new_status,
                    "recovery_at": now.isoformat(),
                }),
            },
        )).fetchall()
        resolved_alerts += len(closed)
        recovered = _recovery_map(closed)
        # How long was it down? A recovery notice that cannot say "for how
        # long" makes the reader go and look it up in the dashboard.
        outage_start = min((r.triggered_at for r in closed if r.triggered_at), default=None)
        outage_duration = duration_between(outage_start, now)
        variables["duration"] = outage_duration

    # E1: live metric values for condition evaluation. packet_loss arrives from
    # the poller as a fraction (0..1); rule thresholds are in percent, so scale.
    metric_values = {
        "ping_status": 1.0 if event.new_status == "up" else 0.0,
        "rtt": float(event.rtt_ms or 0.0),
        "packet_loss": float(event.packet_loss or 0.0) * 100.0,
    }

    for rule in rules:
        trigger = rule.trigger_on or "any"
        if is_recovery and trigger != "up":
            # A reset belongs to the trigger it closes: only rules whose own
            # alert this recovery just resolved run their reset actions. This
            # also lets a 'down' rule send its reset — the trigger_on check
            # below used to skip it, so Router-Down rules never announced the
            # all-clear. ('up' rules are outside this: the up-transition IS
            # their trigger, not a reset.)
            if not rule.recovery_alert:
                continue
            if str(rule.id) not in recovered:
                continue
            variables["duration"] = duration_between(recovered[str(rule.id)]["since"], now)
        else:
            # Check trigger_on match
            if trigger == "down" and not is_down:
                continue
            if trigger == "up" and event.new_status != "up":
                continue
            if trigger == "degraded" and event.new_status != "degraded":
                continue
            # "any" matches everything
            if is_recovery:
                if not rule.recovery_alert:
                    continue
                variables["duration"] = outage_duration

        # Check scope - device_id
        if rule.device_id and str(rule.device_id) != event.device_id:
            continue

        # Check scope - group_id
        if rule.group_id and str(rule.group_id) != group_id:
            continue

        # Check scope - device_type
        if rule.device_type and rule.device_type != device_type:
            continue

        # Check scope - location
        if rule.location and location and rule.location.lower() not in location.lower():
            continue

        # Check scope - device tag. Tags are matched case-insensitively
        # because the registry canonicalises spelling but older assignments
        # in devices.tags may predate that.
        if rule.scope_tag and rule.scope_tag.strip().lower() not in device_tags:
            continue

        # E1: metric-threshold gating. Recovery events are never gated (so they
        # can still resolve open alerts); pure status rules always pass.
        if not is_recovery and not _conditions_match(rule, metric_values):
            continue

        # Rule matches! Send notifications
        variables["severity"] = (rule.severity or "warning").upper()
        variables["rule_name"] = rule.name or "Alert"
        # Per-rule phrasing: the condition sentence depends on which rule
        # matched, so it is rebuilt here rather than with the shared variables.
        variables.update(rule_phrasing(
            rule, hostname=event.hostname, is_recovery=is_recovery,
            reading=variables["rtt"] if (rule.metric or "ping_status") == "rtt" else None,
            duration=variables.get("duration", ""),
        ))

        if suppressing_dependency and not is_recovery:
            await db.execute(
                text("""
                    INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, resolved_at, metadata)
                    VALUES (:device_id, :rule_id, 'resolved', :severity, :message, :triggered_at, :resolved_at, CAST(:metadata AS jsonb))
                """),
                {
                    "device_id": event.device_id,
                    "rule_id": str(rule.id),
                    "severity": rule.severity or "warning",
                    "message": (
                        f"Suppressed downstream alert: {event.hostname} is {event.new_status}. "
                        f"Upstream {suppressing_dependency['parent_hostname']} is "
                        f"{suppressing_dependency['parent_status']}."
                    ),
                    "triggered_at": now,
                    "resolved_at": now,
                    "metadata": json.dumps({
                        "old_status": event.old_status,
                        "new_status": event.new_status,
                        "suppressed_by_dependency": True,
                        "dependency_id": str(suppressing_dependency["dependency_id"]),
                        "parent_device_id": str(suppressing_dependency["parent_device_id"]),
                        "parent_hostname": suppressing_dependency["parent_hostname"],
                        "parent_status": suppressing_dependency["parent_status"],
                        "dependency_type": suppressing_dependency["dependency_type"],
                    }),
                },
            )
            suppressed_alerts += 1
            continue

        # Build messages from templates. A rule that stores no template falls
        # back to the shared defaults in alert_phrasing, which is the same text
        # the preview dialog renders — the two used to disagree, and the mail
        # that actually landed was the worse of the two.
        if is_recovery:
            # No falling back to the trigger body here: "core-router-01 is
            # DOWN" is not a resolved notice, and that fallback is how it used
            # to end up in one.
            email_subject = _render(effective_template(rule.recovery_email_subject, DEFAULT_RECOVERY_EMAIL_SUBJECT), variables)
            email_body = _render(effective_template(rule.recovery_email_body, DEFAULT_RECOVERY_EMAIL_BODY), variables)
            sms_body = _render(effective_template(rule.recovery_sms_template, DEFAULT_RECOVERY_SMS), variables)
        else:
            email_subject = _render(effective_template(rule.email_subject, DEFAULT_EMAIL_SUBJECT), variables)
            email_body = _render(effective_template(rule.email_body, DEFAULT_EMAIL_BODY), variables)
            sms_body = _render(effective_template(rule.sms_template, DEFAULT_SMS), variables)

        if not is_recovery:
            # Respect an active snooze for this rule/device condition: the
            # operator explicitly asked not to be re-alerted on it.
            silenced = (await db.execute(
                text("""SELECT 1 FROM alert_silences
                        WHERE device_id = :did AND dedupe = :d
                          AND (until IS NULL OR until > NOW()) LIMIT 1"""),
                {"did": event.device_id, "d": f"rule:{rule.id}"},
            )).first()
            if silenced:
                suppressed_alerts += 1
                continue

            # A new status supersedes this rule's previous open status alert
            # (down -> degraded and back used to stack a fresh active row per
            # transition, none of which ever closed).
            superseded = await db.execute(
                text("""
                    UPDATE alerts
                    SET status = 'resolved', resolved_at = :now,
                        metadata = COALESCE(metadata, '{}'::jsonb) ||
                                   CAST(:m AS jsonb)
                    WHERE status IN ('active', 'acknowledged')
                      AND device_id = :device_id AND rule_id = :rule_id
                      AND COALESCE(metadata->>'new_status', '') IN ('down', 'degraded')
                """),
                {"now": now, "device_id": event.device_id, "rule_id": str(rule.id),
                 "m": json.dumps({"superseded_by_status": event.new_status,
                                  "resolved_by": "status_transition"})},
            )
            resolved_alerts += superseded.rowcount or 0

        # Create alert record in DB. The stored message is a clean human
        # sentence — the rendered SMS/email templates are transport payloads
        # only (persisting sms_body here put "[ZenPlus WARNING] ..." template
        # text all over the alert UIs).
        inserted = await db.execute(
            text("""
                INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, resolved_at, metadata)
                VALUES (:device_id, :rule_id, :status, :severity, :message, :triggered_at, :resolved_at, CAST(:metadata AS jsonb))
                RETURNING id
            """),
            {
                "device_id": event.device_id,
                "rule_id": str(rule.id),
                "status": "resolved" if is_recovery else "active",
                "severity": rule.severity or "warning",
                "message": (
                    f"{rule.name}: {event.hostname} ({event.ip_address}) recovered — UP"
                    if is_recovery else
                    f"{rule.name}: {event.hostname} ({event.ip_address}) is {event.new_status.upper()}"
                ),
                "triggered_at": now,
                "resolved_at": now if is_recovery else None,
                "metadata": json.dumps({"old_status": event.old_status, "new_status": event.new_status, "is_recovery": is_recovery}),
            },
        )

        new_alert_id = (inserted.first() or [None])[0]

        # Quiet hours: suppress outbound notifications outside the rule's
        # schedule window. The alert row above is always recorded regardless.
        if not is_recovery:
            allowed = notifications_allowed(
                rule.schedule_start, rule.schedule_end,
                getattr(rule, "schedule_days", None), _tz,
            )
            await ns.stamp(db, new_alert_id, allowed)
            if not allowed:
                continue
        # A recovery follows the trigger's fate: an all-clear for a page nobody
        # received is noise about an event they never heard of.
        elif trigger != "up" and not recovered[str(rule.id)]["notified"]:
            continue

        # Send notifications to channels
        channel_ids = rule.notify_channels or []
        for ch_id in channel_ids:
            try:
                ch_result = await db.execute(
                    text("SELECT type, config, gateway_id, enabled FROM notification_channels WHERE id = :id"),
                    {"id": ch_id},
                )
                ch = ch_result.first()
                if not ch or not ch.enabled:
                    continue

                ch_config = ch.config or {}

                if ch.type == "sms":
                    phones = ch_config.get("phone_numbers", "")
                    if not phones:
                        continue

                    # Get SMS gateway
                    gw_id = ch.gateway_id or ch_config.get("gateway_id")
                    if gw_id:
                        gw_res = await db.execute(text("SELECT config FROM notification_gateways WHERE id = :id"), {"id": gw_id})
                    else:
                        gw_res = await db.execute(text("SELECT config FROM notification_gateways WHERE type = 'sms' AND is_default = true LIMIT 1"))
                    gw_row = gw_res.first()
                    if gw_row:
                        # Replace {message} in gateway template with our alert SMS
                        gw_cfg = dict(gw_row.config)
                        tpl = gw_cfg.get("request_template", "")
                        tpl = tpl.replace("{hostname}", event.hostname).replace("{ip_address}", event.ip_address).replace("{status}", event.new_status.upper())
                        gw_cfg["request_template"] = tpl
                        await _send_sms(gw_cfg, phones, sms_body)
                        notifications_sent += 1

                elif ch.type == "email":
                    recipients = ch_config.get("recipients", "")
                    if not recipients:
                        continue

                    gw_conf = await _smtp_gateway(db, ch.gateway_id or ch_config.get("gateway_id"))
                    if gw_conf:
                        email_html = build_alert_email_html({
                            "severity": rule.severity or "warning",
                            "resolved": is_recovery,
                            "status": variables.get("status"),
                            "title": rule.name or "Alert",
                            "hostname": event.hostname,
                            "ip_address": event.ip_address,
                            "message": email_body,
                            # Lead with what actually fired, then the context.
                            "headline_metric": {
                                "label": "Round-trip time",
                                "value": variables.get("rtt"),
                                "secondary_label": "Packet loss",
                                "secondary_value": variables.get("packet_loss"),
                            },
                            "details": _clean_details([
                                ("Alert rule", rule.name),
                                ("Condition", variables.get("condition_label")),
                                # Only on the way back up, and the first thing
                                # anyone asks about a resolved incident.
                                ("Active for", variables.get("duration") if is_recovery else None),
                                ("Round-trip time", variables.get("rtt")),
                                ("Packet loss", variables.get("packet_loss")),
                                ("Group", variables.get("group")),
                                ("Location", variables.get("location")),
                                ("Device type", variables.get("device_type")),
                            ]),
                            "action_url": await _dashboard_url(db, "/alerts"),
                            "timestamp": now.isoformat(),
                        })
                        await _send_email(gw_conf, recipients, email_subject,
                                          email_body, html_body=email_html)
                        notifications_sent += 1

                elif ch.type in ("webhook", "slack", "teams", "discord", "pagerduty"):
                    # Shared context for every outbound provider.
                    ctx = {
                        "hostname":     event.hostname,
                        "ip_address":   event.ip_address,
                        "status":       (event.new_status or "").upper(),
                        "severity":     rule.severity or "warning",
                        "message":      sms_body,
                        "triggered_at": now.isoformat(),
                        "is_recovery":  is_recovery,
                        "device_id":    event.device_id,
                        "rule_id":      str(rule.id),
                        "rule_name":    rule.name,
                    }
                    if await _dispatch_channel(ch.type, ch_config, ctx):
                        notifications_sent += 1

            except Exception as exc:
                # Log but don't fail the whole evaluation
                print(f"ERROR sending notification to channel {ch_id}: {exc}")

    await db.commit()

    # Per-map webhook fan-out: if the device sits on any manual map with a
    # webhook configured, POST a compact transition event. We do this OUT
    # of the rule loop so it fires even when no alert rule matched — the
    # map owner asked to be told about *every* flip on their canvas.
    if event.old_status != event.new_status:
        try:
            map_hooks = (await db.execute(
                text("""
                    SELECT m.id, m.name, m.webhook_url
                    FROM manual_maps m
                    JOIN manual_map_nodes mn ON mn.map_id = m.id
                    WHERE mn.device_id = :did
                      AND m.webhook_enabled = TRUE
                      AND m.webhook_url IS NOT NULL
                      AND m.webhook_url <> ''
                """),
                {"did": event.device_id},
            )).all()
            for row in map_hooks:
                payload = {
                    "event":       "device_status_change",
                    "map_id":      str(row.id),
                    "map_name":    row.name,
                    "device_id":   event.device_id,
                    "hostname":    event.hostname,
                    "ip_address":  event.ip_address,
                    "old_status":  event.old_status,
                    "new_status":  event.new_status,
                    "is_recovery": is_recovery,
                    "timestamp":   now.isoformat(),
                }
                try:
                    async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                        await client.post(row.webhook_url, json=payload)
                except Exception as exc:
                    print(f"ERROR map webhook {row.id}: {exc}")
        except Exception as exc:
            print(f"ERROR map webhook fan-out: {exc}")

    return {
        "evaluated_rules": len(rules),
        "notifications_sent": notifications_sent,
        "device": event.hostname,
        "old_status": event.old_status,
        "new_status": event.new_status,
        "resolved_alerts": resolved_alerts,
        "suppressed_alerts": suppressed_alerts,
    }


@router.post("/evaluate-service")
async def evaluate_service_status_change(
    event: ServiceStatusChangeEvent,
    db: AsyncSession = Depends(get_db),
):
    """
    Called by the Go poller when a service check status changes.
    Evaluates alert rules scoped to this check (by id, service group, or tag
    via metric='service_status') and fires notifications. Mirrors
    /evaluate for devices; internal endpoint, no auth.
    """
    now = datetime.now(timezone.utc)
    is_recovery = event.new_status == "up" and event.old_status in ("down", "degraded", "warning")
    is_down = event.new_status in ("down", "degraded", "warning")

    rules_result = await db.execute(
        text("""
            SELECT id, name, trigger_on, recovery_alert, severity,
                   service_check_id, service_check_group_id, metric,
                   notify_channels, cooldown,
                   email_subject, email_body, sms_template,
                   recovery_email_subject, recovery_email_body, recovery_sms_template,
                   schedule_start, schedule_end, schedule_days
            FROM alert_rules
            WHERE enabled = true
              AND (service_check_id IS NOT NULL OR service_check_group_id IS NOT NULL OR metric = 'service_status')
        """)
    )
    rules = rules_result.fetchall()

    target = event.target or event.check_name
    variables = {
        "hostname": event.device_hostname or event.check_name,
        "check_name": event.check_name,
        "check_type": event.check_type,
        "target": target,
        "ip_address": target,
        "status": event.new_status.upper(),
        "severity": "",
        "rule_name": "",
        "group": event.group_name or "",
        "location": "",
        "device_type": "",
        "metric": "service_status",
        "operator": "==",
        "threshold": "0",
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "duration": "",
        "duration_sentence": "",
        "duration_suffix": "",
        "rtt": f"{event.response_ms:.1f}ms",
        "packet_loss": "",
        "error": event.error or "",
        "status_intro": "The following service alert has been resolved:"
            if is_recovery else "A service alert has been triggered:",
    }

    notifications_sent = 0
    resolved_alerts = 0
    suppressed_alerts = 0
    _tz = await get_configured_timezone(db)
    suppressing_dependency = await _find_suppressing_dependency(db, event.device_id) if (is_down and event.device_id) else None

    # Recovery closes every open alert on this check up front — this used to
    # run per-rule after the trigger_on gate, so a 'down' rule's alerts (the
    # common case) were never resolved by the recovery that ended them.
    recovered: dict[str, dict] = {}
    outage_duration = ""
    if is_recovery:
        closed = (await db.execute(
            text("""
                UPDATE alerts
                SET status = 'resolved',
                    resolved_at = :resolved_at,
                    metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:resolution_metadata AS jsonb)
                WHERE status IN ('active', 'acknowledged')
                  AND service_check_id = :service_check_id
                  AND COALESCE(metadata->>'is_recovery', 'false') != 'true'
                  AND COALESCE(metadata->>'new_status', '') IN ('down', 'degraded', 'warning')
                RETURNING rule_id, triggered_at, metadata->>'notified' AS notified
            """),
            {
                "resolved_at": now,
                "service_check_id": event.service_check_id,
                "resolution_metadata": json.dumps({
                    "resolved_by_recovery": True,
                    "recovery_status": event.new_status,
                    "recovery_at": now.isoformat(),
                }),
            },
        )).fetchall()
        resolved_alerts += len(closed)
        recovered = _recovery_map(closed)
        # Same question as for a device: how long was the check failing?
        outage_start = min((r.triggered_at for r in closed if r.triggered_at), default=None)
        outage_duration = duration_between(outage_start, now)
        variables["duration"] = outage_duration

    for rule in rules:
        trigger = rule.trigger_on or "any"
        if is_recovery and trigger != "up":
            # As on the device path: reset actions run only for the rule whose
            # own alert this recovery just closed.
            if not rule.recovery_alert:
                continue
            if str(rule.id) not in recovered:
                continue
            variables["duration"] = duration_between(recovered[str(rule.id)]["since"], now)
        else:
            if trigger == "down" and not is_down:
                continue
            if trigger == "up" and event.new_status != "up":
                continue
            if trigger == "degraded" and event.new_status != "degraded":
                continue
            if is_recovery:
                if not rule.recovery_alert:
                    continue
                variables["duration"] = outage_duration

        # Scope: service_check_id
        if rule.service_check_id and str(rule.service_check_id) != event.service_check_id:
            continue
        # Scope: service_check_group_id
        if rule.service_check_group_id and str(rule.service_check_group_id) != (event.group_id or ""):
            continue

        variables["severity"] = (rule.severity or "warning").upper()
        variables["rule_name"] = rule.name or "Alert"

        if suppressing_dependency and not is_recovery:
            await db.execute(
                text("""
                    INSERT INTO alerts (
                        device_id, service_check_id, rule_id, status, severity,
                        message, triggered_at, resolved_at, metadata
                    )
                    VALUES (
                        :device_id, :service_check_id, :rule_id, 'resolved', :severity,
                        :message, :triggered_at, :resolved_at, CAST(:metadata AS jsonb)
                    )
                """),
                {
                    "device_id": event.device_id,
                    "service_check_id": event.service_check_id,
                    "rule_id": str(rule.id),
                    "severity": rule.severity or "warning",
                    "message": (
                        f"Suppressed downstream service alert: {event.check_name} is {event.new_status}. "
                        f"Upstream {suppressing_dependency['parent_hostname']} is "
                        f"{suppressing_dependency['parent_status']}."
                    ),
                    "triggered_at": now,
                    "resolved_at": now,
                    "metadata": json.dumps({
                        "old_status": event.old_status,
                        "new_status": event.new_status,
                        "suppressed_by_dependency": True,
                        "dependency_id": str(suppressing_dependency["dependency_id"]),
                        "parent_device_id": str(suppressing_dependency["parent_device_id"]),
                        "parent_hostname": suppressing_dependency["parent_hostname"],
                        "parent_status": suppressing_dependency["parent_status"],
                        "dependency_type": suppressing_dependency["dependency_type"],
                        "check_type": event.check_type,
                        "error": event.error,
                    }),
                },
            )
            suppressed_alerts += 1
            continue

        # A service check reports its own failure reason, so the phrasing leads
        # with the check and the error rather than with a threshold.
        variables["error_sentence"] = f" {event.error.strip().rstrip('.')}." if event.error else ""
        if is_recovery:
            email_subject = _render(effective_template(rule.recovery_email_subject, DEFAULT_RECOVERY_EMAIL_SUBJECT), variables)
            email_body = _render(effective_template(
                rule.recovery_email_body,
                "The {check_type} check “{check_name}” on {target} is passing again."
                "{duration_sentence}"), variables)
            sms_body = _render(effective_template(
                rule.recovery_sms_template,
                "ZenPlus resolved — {rule_name}: {check_name} is passing again."
                "{duration_suffix}"), variables)
        else:
            email_subject = _render(effective_template(rule.email_subject, "[{severity}] {status}: {check_name}"), variables)
            email_body = _render(effective_template(
                rule.email_body,
                "The {check_type} check “{check_name}” on {target} is {status}."
                "{error_sentence}"), variables)
            sms_body = _render(effective_template(
                rule.sms_template,
                "ZenPlus {severity} — {rule_name}: {check_name} is {status} ({target})."),
                variables)

        inserted = await db.execute(
            text("""
                INSERT INTO alerts (device_id, service_check_id, rule_id, status, severity, message, triggered_at, resolved_at, metadata)
                VALUES (:device_id, :service_check_id, :rule_id, :status, :severity, :message, :triggered_at, :resolved_at, CAST(:metadata AS jsonb))
                RETURNING id
            """),
            {
                "device_id": event.device_id,
                "service_check_id": event.service_check_id,
                "rule_id": str(rule.id),
                "status": "resolved" if is_recovery else "active",
                "severity": rule.severity or "warning",
                "message": sms_body,
                "triggered_at": now,
                "resolved_at": now if is_recovery else None,
                "metadata": json.dumps({
                    "old_status": event.old_status,
                    "new_status": event.new_status,
                    "is_recovery": is_recovery,
                    "check_type": event.check_type,
                    "error": event.error,
                }),
            },
        )

        # Quiet hours, and a recovery follows its trigger's fate (see device path).
        svc_alert_id = (inserted.first() or [None])[0]
        if not is_recovery:
            allowed = notifications_allowed(
                rule.schedule_start, rule.schedule_end,
                getattr(rule, "schedule_days", None), _tz,
            )
            await ns.stamp(db, svc_alert_id, allowed)
            if not allowed:
                continue
        elif trigger != "up" and not recovered[str(rule.id)]["notified"]:
            continue

        channel_ids = rule.notify_channels or []
        for ch_id in channel_ids:
            try:
                ch_result = await db.execute(
                    text("SELECT type, config, gateway_id, enabled FROM notification_channels WHERE id = :id"),
                    {"id": ch_id},
                )
                ch = ch_result.first()
                if not ch or not ch.enabled:
                    continue

                ch_config = ch.config or {}

                if ch.type == "sms":
                    phones = ch_config.get("phone_numbers", "")
                    if not phones:
                        continue
                    gw_id = ch.gateway_id or ch_config.get("gateway_id")
                    if gw_id:
                        gw_res = await db.execute(text("SELECT config FROM notification_gateways WHERE id = :id"), {"id": gw_id})
                    else:
                        gw_res = await db.execute(text("SELECT config FROM notification_gateways WHERE type = 'sms' AND is_default = true LIMIT 1"))
                    gw_row = gw_res.first()
                    if gw_row:
                        await _send_sms(dict(gw_row.config), phones, sms_body)
                        notifications_sent += 1

                elif ch.type == "email":
                    recipients = ch_config.get("recipients", "")
                    if not recipients:
                        continue
                    gw_conf = await _smtp_gateway(db, ch.gateway_id or ch_config.get("gateway_id"))
                    if gw_conf:
                        email_html = build_alert_email_html({
                            "severity": rule.severity or "warning",
                            "resolved": is_recovery,
                            "status": variables.get("status"),
                            "title": rule.name or "Service alert",
                            "hostname": variables.get("check_name") or variables.get("hostname"),
                            "message": email_body,
                            "headline_metric": {
                                "label": (variables.get("check_type") or "Check").upper(),
                                "value": variables.get("status"),
                                "secondary_label": "Target",
                                "secondary_value": variables.get("target"),
                            },
                            "details": _clean_details([
                                ("Alert rule", rule.name),
                                ("Check", variables.get("check_name")),
                                ("Type", variables.get("check_type")),
                                ("Target", variables.get("target")),
                                ("Failing for", variables.get("duration") if is_recovery else None),
                                ("Error", variables.get("error")),
                            ]),
                            "action_url": await _dashboard_url(db, "/services"),
                            "timestamp": now.isoformat(),
                        })
                        await _send_email(gw_conf, recipients, email_subject,
                                          email_body, html_body=email_html)
                        notifications_sent += 1

            except Exception as exc:
                print(f"ERROR sending service notification to channel {ch_id}: {exc}")

    await db.commit()

    return {
        "evaluated_rules": len(rules),
        "notifications_sent": notifications_sent,
        "check": event.check_name,
        "old_status": event.old_status,
        "new_status": event.new_status,
        "resolved_alerts": resolved_alerts,
        "suppressed_alerts": suppressed_alerts,
    }


def _trap_oid_matches(rule_filter: Optional[str], trap_oid: str) -> bool:
    """Empty/NULL filter matches any trap; otherwise exact or dotted-prefix match."""
    flt = (rule_filter or "").strip().lstrip(".")
    if not flt:
        return True
    oid = (trap_oid or "").strip().lstrip(".")
    return oid == flt or oid.startswith(flt + ".")


@router.post("/evaluate-trap")
async def evaluate_trap(
    event: TrapEvent,
    db: AsyncSession = Depends(get_db),
):
    """
    Called by the Go poller's trap listener for every received SNMP trap.
    Fires metric='trap' alert rules whose optional OID filter and device/group
    scope match. ALERTS-ONLY: this writes alert rows (visible in the Alerts UI)
    and deliberately does NOT dispatch to notification channels yet.
    """
    now = datetime.now(timezone.utc)

    did = event.device_id or None
    if did in ("", "00000000-0000-0000-0000-000000000000"):
        did = None

    group_id = None
    device_tags: set[str] = set()
    if did:
        dev = (await db.execute(
            text("SELECT group_id, tags FROM devices WHERE id = :id"), {"id": did}
        )).first()
        group_id = str(dev.group_id) if dev and dev.group_id else None
        device_tags = _tag_set(dev.tags if dev else None)

        # Planned downtime: traps from a device in maintenance don't raise alerts.
        if await _device_in_maintenance(db, did):
            return {"alerts_created": 0, "suppressed": "maintenance"}

    rules = (await db.execute(
        text("""
            SELECT id, name, severity, device_id, group_id, scope_tag, trap_oid
            FROM alert_rules
            WHERE enabled = true AND metric = 'trap'
        """)
    )).fetchall()

    alerts_created = 0
    for rule in rules:
        if rule.device_id and (not did or str(rule.device_id) != did):
            continue
        if rule.group_id and (not group_id or str(rule.group_id) != group_id):
            continue
        # A tag-scoped rule must not fall through to every device: a trap from
        # an unidentified source (no device_id) has no tags to match.
        if rule.scope_tag and rule.scope_tag.strip().lower() not in device_tags:
            continue
        if not _trap_oid_matches(rule.trap_oid, event.trap_oid):
            continue

        # Respect an active snooze for this rule on this device.
        if did:
            silenced = (await db.execute(
                text("""SELECT 1 FROM alert_silences
                        WHERE device_id = :did AND dedupe = :d
                          AND (until IS NULL OR until > NOW()) LIMIT 1"""),
                {"did": did, "d": f"rule:{rule.id}"},
            )).first()
            if silenced:
                continue

        label = event.trap_name or event.trap_oid
        message = event.message or f"SNMP trap {label} from {event.source_ip}"
        await db.execute(
            text("""
                INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, metadata)
                VALUES (:device_id, :rule_id, 'active', :severity, :message, :triggered_at, CAST(:metadata AS jsonb))
            """),
            {
                "device_id": did,
                "rule_id": str(rule.id),
                "severity": rule.severity or event.severity or "warning",
                "message": message,
                "triggered_at": now,
                "metadata": json.dumps({
                    "trap": True,
                    "trap_oid": event.trap_oid,
                    "trap_name": event.trap_name,
                    "source_ip": event.source_ip,
                    "trap_severity": event.severity,
                }),
            },
        )
        alerts_created += 1

    await db.commit()
    return {
        "matched_rules": len(rules),
        "alerts_created": alerts_created,
        "trap_oid": event.trap_oid,
        "channels": "skipped (alerts-only)",
    }
