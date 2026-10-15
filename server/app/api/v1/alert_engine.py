from app.services.notification_transport import NotificationHTTPClient
from app.services.sms_transport import fill_template, send_sms_request
from app.services import alert_delivery_log as dl
from app.services.alert_delivery_log import DeliveryRejected, ensure_accepted
from app.core.crypto import encrypt_config, decrypt_config
import ssl
"""
Alert evaluation engine - called by the Go poller on status changes.
Evaluates matching alert rules and sends notifications.
"""
import json
import httpx
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from uuid import UUID
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.database import get_db
from app.services.email_render import build_alert_email_html
from app.services.tag_service import tag_set as _tag_set
from app.services import alert_notify_state as ns
from app.services import device_status_machine as dsm
from app.services.alert_schedule import notifications_allowed, get_configured_timezone
from app.services.alert_phrasing import (
    DEFAULT_EMAIL_BODY, DEFAULT_EMAIL_SUBJECT, DEFAULT_RECOVERY_EMAIL_BODY,
    DEFAULT_RECOVERY_EMAIL_SUBJECT, DEFAULT_RECOVERY_SMS, DEFAULT_SMS,
    conditions_label, default_templates, duration_between, effective_template,
    host_label, rule_phrasing,
)
from pydantic import BaseModel

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


async def require_internal_caller(request: Request) -> None:
    """Engine endpoints are for the local poller only.

    They carry no user authentication — a forged "up" transition would close
    real outages and a forged "down" would page people — yet nginx proxies all
    of /api/ to this app. The poller calls http://localhost:8000 directly;
    anything relayed by nginx arrives from loopback too but always carries
    X-Real-IP / X-Forwarded-For, so those are refused. 404 so the surface is
    not advertised.
    """
    host = request.client.host if request.client else ""
    relayed = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
    if host not in _LOOPBACK or relayed:
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(prefix="/alert-engine", tags=["Alert Engine (Internal)"],
                   dependencies=[Depends(require_internal_caller)])


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
    # Flap state at the check that produced this transition (both optional, so
    # producers from before flap detection still validate). ``flapping``: this
    # transition's own class (reachability or performance) is flapping, which
    # holds its triggers; ``is_flapping``: the device flag, for consumers that
    # only want to label the event. The event carries it rather than the
    # engine reading devices.is_flapping: a sensor outbox replay can lag the
    # device row, which may already describe a later check.
    flapping: bool = False
    is_flapping: bool = False


class TrapEvent(BaseModel):
    event_id: Optional[UUID] = None
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
    return decrypt_config(row.config)


async def _sms_gateway(db: AsyncSession, gw_id) -> Optional[dict]:
    """SMS twin of _smtp_gateway: a gateway disabled on the Gateways page must
    stop delivery (the engine loops used to read the config blob regardless)."""
    if gw_id:
        row = (await db.execute(
            text("SELECT config, enabled FROM notification_gateways WHERE id = :id"),
            {"id": gw_id})).first()
    else:
        row = (await db.execute(text(
            "SELECT config, enabled FROM notification_gateways "
            "WHERE type = 'sms' AND is_default = true LIMIT 1"))).first()
    if not row or not row.enabled:
        return None
    return decrypt_config(row.config)


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


def _sms_fill(template: str, values: dict, *, is_get: bool) -> str:
    """Substitute {placeholders} into an SMS gateway request template.

    GET templates become part of a query string, so substituted values must be
    URL-encoded — an unencoded message body ("CPU > 90% & rising") truncated
    the text at the first ``&`` and could smuggle extra query parameters.
    Recipient lists keep ``,`` and ``+`` literal (multi-number and E.164
    conventions most gateways expect). POST bodies are provider-defined
    (JSON/XML/form), so they are substituted verbatim as before.
    """
    return fill_template(template, values, is_get=is_get)


async def _send_sms(gw_config: dict, phones: str, message: str) -> bool:
    if gw_config.get('provider') != 'custom_http' or not gw_config.get('api_url'):
        return False
    await send_sms_request(gw_config, {
        'recipients': phones, 'message': message,
        'sender': gw_config.get('sender_name') or 'ZenPlus',
    }, client_factory=NotificationHTTPClient)
    return True


async def _send_email(gw_config: dict, recipients: str, subject: str, body: str,
                      html_body: str | None = None) -> bool:
    """Send email via SMTP gateway.

    When ``html_body`` is provided the message is sent as multipart/alternative
    (plain ``body`` + HTML), so clients render the professional HTML template
    while text-only clients still get a readable fallback.
    """
    if not gw_config.get("host"):
        return False

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
        server = smtplib.SMTP_SSL(gw_config["host"], gw_config.get("port", 465), timeout=10, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(gw_config["host"], gw_config.get("port", 587), timeout=10)
        server.starttls(context=ssl.create_default_context())
    if gw_config.get("username"):
        server.login(gw_config["username"], gw_config.get("password", ""))
    refused = server.sendmail(gw_config.get("from_email", ""), recipient_list, msg.as_string())
    server.quit()
    if refused and len(refused) >= len(recipient_list):
        raise DeliveryRejected(f"all {len(refused)} recipient(s) refused")
    return True


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
    async with NotificationHTTPClient(timeout=10.0, verify=ch_config.get("tls_verify", True)) as client:
        if isinstance(payload, str):
            resp = await client.post(url, content=payload, headers=headers)
        else:
            resp = await client.post(url, json=payload, headers=headers)
    ensure_accepted(resp)


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
    async with NotificationHTTPClient(timeout=10.0) as client:
        resp = await client.post(url, json=payload)
    ensure_accepted(resp)


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
    async with NotificationHTTPClient(timeout=10.0) as client:
        resp = await client.post(url, json=card)
    ensure_accepted(resp)


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
    async with NotificationHTTPClient(timeout=10.0) as client:
        resp = await client.post(url, json=payload)
    ensure_accepted(resp)


async def _send_pagerduty(ch_config: dict, ctx: dict) -> None:
    """PagerDuty Events API v2 — trigger / resolve based on is_recovery."""
    routing_key = ch_config.get("routing_key") or ch_config.get("integration_key")
    if not routing_key:
        return
    is_recovery = bool(ctx.get("is_recovery"))
    # Trigger and resolve must share one key per condition. Device alerts key
    # on device_id as before; server/service/host alerts used to collapse into
    # "<rule>:unknown", so one entity's recovery resolved another's incident.
    subject = (ctx.get("device_id") or ctx.get("server_id") or ctx.get("service_check_id")
               or ctx.get("hostname") or "unknown")
    dedup_key = ctx.get("dedup_key") or f"zenplus:{ctx.get('rule_id', 'unknown')}:{subject}"
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
    async with NotificationHTTPClient(timeout=10.0) as client:
        resp = await client.post("https://events.pagerduty.com/v2/enqueue", json=payload)
    ensure_accepted(resp)


_HTTP_SENDERS = {
    "webhook": lambda cfg, ctx: _send_webhook(cfg, ctx),
    "slack": lambda cfg, ctx: _send_slack(cfg, ctx),
    "teams": lambda cfg, ctx: _send_teams(cfg, ctx),
    "discord": lambda cfg, ctx: _send_discord(cfg, ctx),
    "pagerduty": lambda cfg, ctx: _send_pagerduty(cfg, ctx),
}


async def _dispatch_channel_result(ch_type: str, ch_config: dict, ctx: dict):
    """(accepted, error) for one webhook-family channel. accepted=False with
    error=None means the type is unsupported/unconfigured (nothing sent)."""
    sender = _HTTP_SENDERS.get(ch_type)
    if not sender:
        return False, None
    if not (ch_config.get("url") or ch_config.get("webhook_url")
            or ch_config.get("routing_key") or ch_config.get("integration_key")):
        return False, None
    try:
        await sender(ch_config, ctx)
        return True, None
    except Exception as exc:
        print(f"ERROR dispatch {ch_type}: {exc}")
        return False, exc


async def _dispatch_channel(ch_type: str, ch_config: dict, ctx: dict) -> bool:
    """Single dispatch point. Returns True if the provider accepted it."""
    ok, _err = await _dispatch_channel_result(ch_type, ch_config, ctx)
    return ok


async def _log_delivery(db: AsyncSession, alert_id, rule, ch_id, ch, is_recovery: bool,
                        outcome: str, error=None, latency_ms: int | None = None) -> None:
    await dl.record(
        db, alert_id=alert_id, rule_id=getattr(rule, "id", None), channel_id=ch_id,
        channel_name=getattr(ch, "name", None), channel_type=getattr(ch, "type", None),
        kind="recovery" if is_recovery else "trigger", outcome=outcome, error=error,
        latency_ms=latency_ms if outcome != "skipped" else None)


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
            out[rid] = {"notified": sent, "since": row.triggered_at,
                        "alert_id": getattr(row, "id", None)}
            continue
        entry["notified"] = entry["notified"] or sent
        if entry.get("alert_id") is None:
            entry["alert_id"] = getattr(row, "id", None)
        if row.triggered_at and (entry["since"] is None or row.triggered_at < entry["since"]):
            entry["since"] = row.triggered_at
    return out


_PENDING_STATUS_SUPPORTED: bool | None = None


async def _pending_supported(db: AsyncSession) -> bool:
    """Whether alerts.status accepts 'pending' (migrate-103).

    min_duration holds park the alert row as status='pending' until the hold
    sweeper confirms the condition survived. On a DB whose CHECK constraint
    predates migrate-103 that insert would blow up the whole evaluation, so
    holds silently degrade to the old fire-immediately behaviour until the
    migration runs.
    """
    global _PENDING_STATUS_SUPPORTED
    if _PENDING_STATUS_SUPPORTED is None:
        _PENDING_STATUS_SUPPORTED = bool((await db.execute(text("""
            SELECT COALESCE(bool_and(pg_get_constraintdef(oid) LIKE '%pending%'), true)
            FROM pg_constraint
            WHERE conrelid = 'alerts'::regclass AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%status%'
        """))).scalar())
        if not _PENDING_STATUS_SUPPORTED:
            print("WARN alert hold (min_duration) disabled: run migrate-103-alert-pending-status.sql")
    return _PENDING_STATUS_SUPPORTED


_NOTIFY_WHILE_FLAPPING_SUPPORTED = False


async def _notify_while_flapping_supported(db: AsyncSession) -> bool:
    """Whether alert_rules.notify_while_flapping exists (migrate-161).

    The rule query below names it, and a missing column would fail every
    device evaluation in the window between a code update and its migration.
    Until it exists every rule reads as False (the column default). A positive
    answer is cached; a negative one is asked again, so the engine picks the
    migration up without a restart.
    """
    global _NOTIFY_WHILE_FLAPPING_SUPPORTED
    if not _NOTIFY_WHILE_FLAPPING_SUPPORTED:
        _NOTIFY_WHILE_FLAPPING_SUPPORTED = bool((await db.execute(text("""
            SELECT EXISTS (SELECT 1 FROM pg_attribute
                           WHERE attrelid = 'alert_rules'::regclass
                             AND attname = 'notify_while_flapping' AND NOT attisdropped)
        """))).scalar())
    return _NOTIFY_WHILE_FLAPPING_SUPPORTED


async def _status_policy(db: AsyncSession) -> dsm.Policy:
    """The effective device status policy (system_settings 'monitoring'),
    through the same loader the poller and the sensor ingest use."""
    row = (await db.execute(
        text("SELECT value FROM system_settings WHERE key = 'monitoring'"))).first()
    raw = row[0] if row else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return dsm.policy_from_settings(raw if isinstance(raw, dict) else None)


async def _within_cooldown(db: AsyncSession, rule, *, device_id: str | None = None,
                           service_check_id: str | None = None,
                           exclude_alert_id=None) -> bool:
    """True when this rule already announced a trigger for this object within
    its cooldown window.

    This is what the wizard's "Cooldown (seconds)" field promises and the
    engine never enforced: a device flapping down/up/down re-paged every
    transition. The alert row is still recorded either way — only the outbound
    notification is suppressed, and only when the previous trigger actually
    went out (a quiet-hours-suppressed one doesn't start a cooldown).
    """
    cd = int(getattr(rule, "cooldown", 0) or 0)
    if cd <= 0:
        return False
    scope = "device_id = :oid" if device_id else "service_check_id = :oid"
    row = (await db.execute(
        text(f"""
            SELECT 1 FROM alerts
            WHERE rule_id = :rid AND {scope}
              AND (CAST(:aid AS uuid) IS NULL OR id <> CAST(:aid AS uuid))
              AND COALESCE(metadata->>'is_recovery', 'false') <> 'true'
              AND COALESCE(metadata->>'notified', 'true') = 'true'
              AND triggered_at > now() - make_interval(secs => :cd)
            LIMIT 1
        """),
        {"rid": str(rule.id), "oid": device_id or service_check_id,
         "aid": str(exclude_alert_id) if exclude_alert_id else None, "cd": cd},
    )).first()
    return row is not None


def _trigger_matches(trigger: str, new_status: str) -> bool:
    """Does a rule's trigger_on own this transition's new state?

    'down' means DOWN only: degraded is its own state with its own trigger and
    recovery pair — lumping it into 'down' made every latency blip page the
    Devices-Down rules. 'degraded' also owns the service checks' 'warning'
    state (same degraded class), so every state has exactly one trigger that
    owns it, and 'any' matches every transition.
    """
    if trigger == "down":
        return new_status == "down"
    if trigger == "up":
        return new_status == "up"
    if trigger == "degraded":
        return new_status in ("degraded", "warning")
    return True


def _ping_status_value(new_status: str) -> float | None:
    """ping_status for a device transition: 0 when down, 1 while it still
    answers (a degraded device responds; latency/loss rules own degraded)."""
    if new_status == "down":
        return 0.0
    if new_status in ("up", "degraded"):
        return 1.0
    return None


def _conditions_match(rule, values: dict) -> bool:
    """
    E1: evaluate a rule's metric condition(s) against live metric values.

    - A non-empty ``conditions`` array is evaluated element-wise and combined by
      ``condition_logic`` (AND/OR).
    - Otherwise the legacy flat metric/operator/threshold is the single
      condition. service_status (and an absent metric) is not metric-gated.

    ping_status is only evaluated on an 'any' rule. An explicit trigger_on
    already names the state, and the wizard stores ping_status == 0 on every
    status rule whatever the trigger, so gating on it would silence "Device
    comes up" and "Device degraded" rules. On an 'any' rule it is the only
    thing selecting the state — skipping it made "Device Down" fire on
    unknown->up and up->degraded.
    """
    status_owned = (getattr(rule, "trigger_on", None) or "any") != "any"

    def one(metric, operator, threshold) -> bool:
        if metric == "ping_status" and status_owned:
            return True
        return _eval_one(metric, operator, threshold, values)

    conds = getattr(rule, "conditions", None)
    if conds:
        results = [one(c.get("metric"), c.get("operator"), c.get("threshold")) for c in conds]
        logic = (getattr(rule, "condition_logic", "AND") or "AND").upper()
        return any(results) if logic == "OR" else all(results)
    metric = getattr(rule, "metric", None)
    if metric in ("service_status", None):
        return True
    return one(metric, getattr(rule, "operator", None), getattr(rule, "threshold", None))


# The metric values a device status transition carries (see metric_values in
# evaluate_status_change). A rule conditioned on anything else — CPU, an
# interface, a trap — can never fire on a status transition.
_STATUS_METRICS = frozenset({"ping_status", "rtt", "packet_loss"})


def _status_path_rule(rule) -> bool:
    """Is this one of the rules a device status transition can fire?

    The status path loads every enabled non-service rule and lets the metric
    gate drop the rest; flap notices have no metric values to gate on, so they
    select the status rules by their conditions instead.
    """
    conds = getattr(rule, "conditions", None)
    if conds:
        return all(isinstance(c, dict) and c.get("metric") in _STATUS_METRICS for c in conds)
    metric = getattr(rule, "metric", None)
    return metric is None or metric in _STATUS_METRICS


def _rule_in_device_scope(rule, *, device_id: str, group_id: str | None,
                          device_type: str | None, location: str | None,
                          device_tags: set[str]) -> bool:
    """A device rule's device / group / type / location / tag scope.

    Shared by the status path and the flap notices so the two can never
    disagree about which rules cover a device.
    """
    if rule.device_id and str(rule.device_id) != device_id:
        return False
    if rule.group_id and str(rule.group_id) != group_id:
        return False
    if rule.device_type and rule.device_type != device_type:
        return False
    if rule.location and location and rule.location.lower() not in location.lower():
        return False
    # Tags are matched case-insensitively because the registry canonicalises
    # spelling but older assignments in devices.tags may predate that.
    if rule.scope_tag and rule.scope_tag.strip().lower() not in device_tags:
        return False
    return True


def _transition_class(old_status: str, new_status: str) -> str:
    """Flap class of a hard change: into or out of down is reachability,
    up<->degraded is performance (docs/device-status-policy.md)."""
    return "reachability" if "down" in (old_status, new_status) else "performance"


_DEVICE_RULE_COLUMNS = """
    id, name, trigger_on, recovery_alert, severity,
    device_id, group_id, device_type, location, scope_tag,
    notify_channels, cooldown, min_duration,
    metric, operator, threshold, conditions, condition_logic,
    email_subject, email_body, sms_template,
    recovery_email_subject, recovery_email_body, recovery_sms_template,
    schedule_start, schedule_end, schedule_days
"""


async def _load_device_rules(db: AsyncSession) -> list:
    """Every enabled rule the device paths consider.

    Service-check rules are excluded: their metric ('service_status') is
    treated as always-matching by the condition gate, so a device ping
    transition used to fire them — "HTTP health: SWITCH is DEGRADED" alerts
    about a check nobody ran.
    """
    flap_col = ("notify_while_flapping" if await _notify_while_flapping_supported(db)
                else "false AS notify_while_flapping")
    return (await db.execute(text(f"""
        SELECT {_DEVICE_RULE_COLUMNS}, {flap_col}
        FROM alert_rules
        WHERE enabled = true
          AND service_check_id IS NULL
          AND service_check_group_id IS NULL
          AND COALESCE(metric, '') <> 'service_status'
    """))).fetchall()


async def _device_in_maintenance(db: AsyncSession, device_id: str) -> bool:
    """True for manual maintenance or an active device_maintenance window.

    Defense in depth: the poller already suppresses transitions for devices in
    maintenance, but this also covers events raised while a window was being
    created, and traps. Tolerates the table not existing yet (rolling upgrade
    before migrate-053 has run).
    """
    try:
        row = await db.execute(
            text("""
                SELECT 1
                FROM devices d
                WHERE d.id = :did AND (d.status = 'maintenance' OR EXISTS (
                    SELECT 1 FROM device_maintenance m WHERE (
                       (m.scope_type = 'device' AND m.scope_device_id = d.id)
                    OR (m.scope_type = 'group'  AND m.scope_group_id = d.group_id)
                    OR (m.scope_type = 'tag'    AND jsonb_exists(COALESCE(d.tags, '[]'::jsonb), m.scope_tag))
                    OR (m.scope_type = 'all')
                )
                AND m.starts_at <= now() AND m.ends_at >= now()
                ))
                LIMIT 1
            """),
            {"did": device_id},
        )
        return row.first() is not None
    except Exception:
        return False


async def _offline_sensor_owner(db: AsyncSession, device_id: str):
    """Return the owning offline sensor, if this device must be treated stale."""
    try:
        return (await db.execute(
            text("""SELECT s.id, s.name, s.last_heartbeat_at
                      FROM device_polling_owner owner
                      JOIN sensors s ON s.id = owner.sensor_id
                     WHERE owner.device_id = :did
                       AND owner.owner_kind = 'sensor'
                       AND s.status = 'offline'
                     LIMIT 1"""),
            {"did": device_id},
        )).mappings().first()
    except Exception:
        # Rolling-upgrade compatibility before migrate-105 is applied.
        return None


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
    # One evaluation per device at a time, until this transaction ends. The
    # poller posts a device's transitions in order, but a timed-out POST keeps
    # running here while the next one arrives, and sensor-outbox workers run
    # alongside: a down->up evaluated before the up->down that preceded it has
    # committed cannot see that row, so it closes nothing and leaves an active
    # DOWN alert on an UP device.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:device_id))"),
                     {"device_id": event.device_id})
    now = datetime.now(timezone.utc)
    # Arriving at 'up' is never a problem. Only down/degraded -> up is a
    # recovery; unknown/maintenance -> up (a new device's first ping, the end
    # of a window) resolves stale status alerts silently.
    into_up = event.new_status == "up"
    is_recovery = into_up and event.old_status in ("down", "degraded")
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
    # maintenance window. Recovery events still pass so open alerts resolve,
    # and any other move to 'up' still closes stale ones (below).
    in_maintenance = not is_recovery and await _device_in_maintenance(db, event.device_id)
    if in_maintenance and not into_up:
        return {"evaluated": 0, "matched": 0, "suppressed": "maintenance"}
    if is_down:
        offline_owner = await _offline_sensor_owner(db, event.device_id)
        if offline_owner:
            return {
                "evaluated": 0,
                "matched": 0,
                "suppressed": "sensor_offline",
                "sensor_id": str(offline_owner["id"]),
                "sensor_name": offline_owner["name"],
            }

    # Get group name
    group_name = ""
    if group_id:
        gr = await db.execute(text("SELECT name FROM device_groups WHERE id = :id"), {"id": group_id})
        gr_row = gr.first()
        group_name = gr_row.name if gr_row else ""

    rules = await _load_device_rules(db)

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

    # Any transition to 'up' closes ALL open status alerts on this device,
    # before any per-rule gating, whatever status they recorded — rows from
    # before the into-up guard ("Device Down: X is UP") close too. This used to
    # live inside the rules loop behind `rule.recovery_alert`, so rules without
    # a recovery notification never resolved their alerts — one firewall accumulated 100+ permanently
    # "active" DEGRADED alerts, poisoning every alert counter in the UI.
    # Service-check alerts are deliberately NOT closed here: the device
    # answering ping again says nothing about its HTTP check passing.
    # A row still in its hold (pending) never became an alert: it closes as a
    # cancelled hold, a notice, not as a recovered incident — otherwise every
    # cycle of a flapping device would count as an incident in the Alert Center.
    recovered: dict[str, dict] = {}
    outage_duration = ""
    if into_up:
        closed = (await db.execute(
            text("""
                UPDATE alerts
                SET status = 'resolved',
                    resolved_at = :resolved_at,
                    metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:resolution_metadata AS jsonb)
                               || CASE WHEN status = 'pending'
                                       THEN jsonb_build_object('hold_cancelled', true)
                                       ELSE '{}'::jsonb END
                WHERE status IN ('pending', 'active', 'acknowledged')
                  AND device_id = :device_id
                  AND service_check_id IS NULL
                  AND COALESCE(metadata->>'is_recovery', 'false') != 'true'
                  AND metadata->>'new_status' IS NOT NULL
                RETURNING id, rule_id, triggered_at, metadata->>'notified' AS notified,
                          metadata->>'new_status' AS new_status,
                          metadata->>'hold_cancelled' AS hold_cancelled
            """),
            {
                "resolved_at": now,
                "device_id": event.device_id,
                "resolution_metadata": json.dumps({
                    "resolved_by_recovery": True,
                    "recovery_status": event.new_status,
                    "recovery_at": now.isoformat(),
                } if is_recovery else {
                    "resolved_by": "status_transition",
                    "superseded_by_status": event.new_status,
                }),
            },
        )).fetchall()
        resolved_alerts += len(closed)
    if in_maintenance:
        await db.commit()
        return {"evaluated": 0, "matched": 0, "suppressed": "maintenance",
                "resolved_alerts": resolved_alerts}
    if is_recovery:
        # Only real outages drive the reset notices and "active for". A hold
        # this recovery cancelled never became an alert, so it has nothing to
        # recover: no reset row, no all-clear (a flapping device would record
        # one per cycle).
        closed = [r for r in closed if r.new_status in ("down", "degraded")
                  and str(getattr(r, "hold_cancelled", None) or "").lower() != "true"]
        recovered = _recovery_map(closed)
        # How long was it down? A recovery notice that cannot say "for how
        # long" makes the reader go and look it up in the dashboard.
        outage_start = min((r.triggered_at for r in closed if r.triggered_at), default=None)
        outage_duration = duration_between(outage_start, now)
        variables["duration"] = outage_duration

    # E1: live metric values for condition evaluation. packet_loss arrives from
    # the poller as a fraction (0..1); rule thresholds are in percent, so scale.
    metric_values = {
        "ping_status": _ping_status_value(event.new_status),
        "rtt": float(event.rtt_ms or 0.0),
        "packet_loss": float(event.packet_loss or 0.0) * 100.0,
    }

    # Flapping: hold, not drop. The flap hold is the policy's stable time,
    # loaded once per evaluation and only when a held trigger needs it.
    flap_hold_s: int | None = None
    flap_held_alerts = 0

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
            if not _trigger_matches(trigger, event.new_status):
                continue
            if into_up and trigger != "up":
                continue
            if is_recovery:
                if not rule.recovery_alert:
                    continue
                variables["duration"] = outage_duration

        if not _rule_in_device_scope(rule, device_id=event.device_id, group_id=group_id,
                                     device_type=device_type, location=location,
                                     device_tags=device_tags):
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
            rule, hostname=host_label(event.hostname, event.ip_address), is_recovery=is_recovery,
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
            # transition, none of which ever closed). A row still in its hold
            # is a cancelled hold, as on the way up.
            superseded = await db.execute(
                text("""
                    UPDATE alerts
                    SET status = 'resolved', resolved_at = :now,
                        metadata = COALESCE(metadata, '{}'::jsonb) ||
                                   CAST(:m AS jsonb)
                                   || CASE WHEN status = 'pending'
                                           THEN jsonb_build_object('hold_cancelled', true)
                                           ELSE '{}'::jsonb END
                    WHERE status IN ('pending', 'active', 'acknowledged')
                      AND device_id = :device_id AND rule_id = :rule_id
                      AND service_check_id IS NULL
                      AND COALESCE(metadata->>'is_recovery', 'false') != 'true'
                      AND metadata->>'new_status' IS NOT NULL
                """),
                {"now": now, "device_id": event.device_id, "rule_id": str(rule.id),
                 "m": json.dumps({"superseded_by_status": event.new_status,
                                  "resolved_by": "status_transition"})},
            )
            resolved_alerts += superseded.rowcount or 0

        # "Condition must exist for" hold: the row is parked as 'pending' —
        # invisible to active-alert counters, nothing dispatched — and the hold
        # sweeper (alert_hold_service, 30s cadence) fires it only if the device
        # is still in this state once min_duration has passed. A recovery in
        # the meantime resolves it silently: a 59-second blip under a
        # 100-second hold produces no alert and no pages.
        hold_s = 0 if is_recovery else int(rule.min_duration or 0)
        # While this transition's class is flapping, a trigger must last the
        # flap stable time before it notifies: dropping it would delay a real
        # outage page until flapping ends, holding it pages a sustained state
        # within that time and lets every blip cancel silently. Recoveries are
        # untouched — they follow their trigger's notified fate, so a trigger
        # paged before the flapping still gets its all-clear and a held one
        # recovers silently. notify_while_flapping opts a rule out (core
        # routers).
        flap_held = (event.flapping and not is_recovery
                     and not getattr(rule, "notify_while_flapping", False))
        if flap_held:
            if flap_hold_s is None:
                flap_hold_s = (await _status_policy(db)).flap_stable_minutes * 60
            hold_s = max(hold_s, flap_hold_s)
        held = hold_s > 0 and await _pending_supported(db)

        # Create alert record in DB. The stored message is a clean human
        # sentence — the rendered SMS/email templates are transport payloads
        # only (persisting sms_body here put "[ZenPlus WARNING] ..." template
        # text all over the alert UIs).
        row_metadata = {"old_status": event.old_status, "new_status": event.new_status, "is_recovery": is_recovery}
        if held:
            row_metadata["notified"] = False
            row_metadata["hold_until"] = (now + timedelta(seconds=hold_s)).isoformat()
            if flap_held:
                row_metadata["flap_held"] = True
                row_metadata["flap_kind"] = _transition_class(event.old_status, event.new_status)
                flap_held_alerts += 1
        inserted = await db.execute(
            text("""
                INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, resolved_at, metadata)
                VALUES (:device_id, :rule_id, :status, :severity, :message, :triggered_at, :resolved_at, CAST(:metadata AS jsonb))
                RETURNING id
            """),
            {
                "device_id": event.device_id,
                "rule_id": str(rule.id),
                "status": "resolved" if is_recovery else ("pending" if held else "active"),
                "severity": rule.severity or "warning",
                "message": (
                    f"{rule.name}: {event.hostname} ({event.ip_address}) recovered — UP"
                    if is_recovery else
                    f"{rule.name}: {event.hostname} ({event.ip_address}) is {event.new_status.upper()}"
                ),
                "triggered_at": now,
                "resolved_at": now if is_recovery else None,
                "metadata": json.dumps(row_metadata),
            },
        )

        new_alert_id = (inserted.first() or [None])[0]
        if held:
            # Quiet hours and cooldown are the sweeper's problem at confirm
            # time — evaluating them now would gate on the wrong moment.
            continue

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
            # Flap cooldown: this rule already paged for this device moments
            # ago. Stamped not-notified so the matching recovery stays quiet.
            if await _within_cooldown(db, rule, device_id=event.device_id,
                                      exclude_alert_id=new_alert_id):
                await ns.stamp(db, new_alert_id, False)
                suppressed_alerts += 1
                continue
        # A recovery follows the trigger's fate: an all-clear for a page nobody
        # received is noise about an event they never heard of.
        elif trigger != "up" and not recovered[str(rule.id)]["notified"]:
            continue

        # Send notifications to channels. Every channel's outcome goes to the
        # delivery log against the incident it is about: the new row for a
        # trigger, the closed incident for a recovery.
        channel_ids = rule.notify_channels or []
        delivery_alert_id = (recovered.get(str(rule.id), {}).get("alert_id")
                             if is_recovery else new_alert_id)
        for ch_id in channel_ids:
            ch = None
            timer = dl.Timer()
            try:
                ch_result = await db.execute(
                    text("SELECT name, type, config, gateway_id, enabled FROM notification_channels WHERE id = :id"),
                    {"id": ch_id},
                )
                ch = ch_result.first()
                if not ch or not ch.enabled:
                    await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery,
                                        "skipped", "config: channel disabled or deleted")
                    continue

                ch_config = decrypt_config(ch.config)

                if ch.type == "sms":
                    phones = ch_config.get("phone_numbers", "")
                    gw_cfg = await _sms_gateway(db, ch.gateway_id or ch_config.get("gateway_id")) if phones else None
                    if not phones or not gw_cfg:
                        await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery, "skipped",
                                            "config: no phone numbers" if not phones else "config: no enabled SMS gateway")
                        continue
                    # Pre-fill device placeholders in the gateway template
                    # (encoded per HTTP method); {message}/{recipients}/
                    # {sender} are filled by _send_sms itself.
                    gw_cfg["request_template"] = fill_template(
                        gw_cfg.get("request_template", ""),
                        {"hostname": event.hostname,
                         "ip_address": event.ip_address,
                         "status": event.new_status.upper()},
                        is_get=gw_cfg.get("http_method", "GET").upper() != "POST",
                        content_type=gw_cfg.get("content_type", ""),
                    )
                    if await _send_sms(gw_cfg, phones, sms_body):
                        notifications_sent += 1
                        await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery,
                                            "accepted", None, timer.ms)
                    else:
                        await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery,
                                            "skipped", "config: SMS gateway is not a usable HTTP gateway")

                elif ch.type == "email":
                    recipients = ch_config.get("recipients", "")
                    gw_conf = await _smtp_gateway(db, ch.gateway_id or ch_config.get("gateway_id")) if recipients else None
                    if not recipients or not gw_conf:
                        await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery, "skipped",
                                            "config: no recipients" if not recipients else "config: no enabled SMTP gateway")
                        continue
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
                        if await _send_email(gw_conf, recipients, email_subject,
                                             email_body, html_body=email_html):
                            notifications_sent += 1
                            await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery,
                                                "accepted", None, timer.ms)
                        else:
                            await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery,
                                                "skipped", "config: SMTP gateway has no host")

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
                        "alert_id":     str(delivery_alert_id) if delivery_alert_id else None,
                        "event_type":   "recovery" if is_recovery else "trigger",
                    }
                    ok, err = await _dispatch_channel_result(ch.type, ch_config, ctx)
                    if ok:
                        notifications_sent += 1
                    await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery,
                                        "accepted" if ok else ("failed" if err else "skipped"),
                                        err if err else (None if ok else "config: no URL or routing key"),
                                        timer.ms)

            except Exception as exc:
                # Log but don't fail the whole evaluation
                print(f"ERROR sending notification to channel {ch_id}: {exc}")
                await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery,
                                    "failed", exc, timer.ms)

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
                    # Still sent on every flip; the receiver decides what a
                    # flip of a flapping device means to its canvas.
                    "is_flapping": event.is_flapping,
                    "timestamp":   now.isoformat(),
                }
                try:
                    async with NotificationHTTPClient(timeout=10.0, verify=True) as client:
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
        "flap_held_alerts": flap_held_alerts,
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
    # As on the device path: arriving at 'up' never opens a problem alert.
    into_up = event.new_status == "up"
    is_recovery = into_up and event.old_status in ("down", "degraded", "warning")
    is_down = event.new_status in ("down", "degraded", "warning")

    rules_result = await db.execute(
        text("""
            SELECT id, name, trigger_on, recovery_alert, severity,
                   service_check_id, service_check_group_id, metric,
                   notify_channels, cooldown, min_duration,
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
    if into_up:
        closed = (await db.execute(
            text("""
                UPDATE alerts
                SET status = 'resolved',
                    resolved_at = :resolved_at,
                    metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:resolution_metadata AS jsonb)
                WHERE status IN ('pending', 'active', 'acknowledged')
                  AND service_check_id = :service_check_id
                  AND COALESCE(metadata->>'is_recovery', 'false') != 'true'
                  AND metadata->>'new_status' IS NOT NULL
                RETURNING id, rule_id, triggered_at, metadata->>'notified' AS notified,
                          metadata->>'new_status' AS new_status
            """),
            {
                "resolved_at": now,
                "service_check_id": event.service_check_id,
                "resolution_metadata": json.dumps({
                    "resolved_by_recovery": True,
                    "recovery_status": event.new_status,
                    "recovery_at": now.isoformat(),
                } if is_recovery else {
                    "resolved_by": "status_transition",
                    "superseded_by_status": event.new_status,
                }),
            },
        )).fetchall()
        resolved_alerts += len(closed)
    if is_recovery:
        closed = [r for r in closed if r.new_status in ("down", "degraded", "warning")]
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
            if not _trigger_matches(trigger, event.new_status):
                continue
            if into_up and trigger != "up":
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
        # Defaults live in alert_phrasing so the rule editors advertise exactly
        # the text this path sends.
        svc_defaults = default_templates("service")
        if is_recovery:
            email_subject = _render(effective_template(rule.recovery_email_subject, svc_defaults["recovery_email_subject"]), variables)
            email_body = _render(effective_template(rule.recovery_email_body, svc_defaults["recovery_email_body"]), variables)
            sms_body = _render(effective_template(rule.recovery_sms_template, svc_defaults["recovery_sms_template"]), variables)
        else:
            email_subject = _render(effective_template(rule.email_subject, svc_defaults["email_subject"]), variables)
            email_body = _render(effective_template(rule.email_body, svc_defaults["email_body"]), variables)
            sms_body = _render(effective_template(rule.sms_template, svc_defaults["sms_template"]), variables)

        # "Condition must exist for" hold, exactly as on the device path: park
        # the row as 'pending' and let the hold sweeper confirm the check is
        # still failing after min_duration before anything is dispatched.
        hold_s = 0 if is_recovery else int(rule.min_duration or 0)
        held = hold_s > 0 and await _pending_supported(db)

        row_metadata = {
            "old_status": event.old_status,
            "new_status": event.new_status,
            "is_recovery": is_recovery,
            "check_type": event.check_type,
            "error": event.error,
        }
        if held:
            row_metadata["notified"] = False
            row_metadata["hold_until"] = (now + timedelta(seconds=hold_s)).isoformat()
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
                "status": "resolved" if is_recovery else ("pending" if held else "active"),
                "severity": rule.severity or "warning",
                # A clean human sentence, as on the device path — the rendered
                # SMS is a transport payload and made every alert list read
                # like "[ZenPlus WARNING] ..." template dumps.
                "message": (
                    f"{rule.name}: {event.check_name} is passing again — recovered"
                    if is_recovery else
                    f"{rule.name}: {event.check_name} ({target}) is {event.new_status.upper()}"
                ),
                "triggered_at": now,
                "resolved_at": now if is_recovery else None,
                "metadata": json.dumps(row_metadata),
            },
        )

        # Quiet hours, and a recovery follows its trigger's fate (see device path).
        svc_alert_id = (inserted.first() or [None])[0]
        if held:
            continue
        if not is_recovery:
            allowed = notifications_allowed(
                rule.schedule_start, rule.schedule_end,
                getattr(rule, "schedule_days", None), _tz,
            )
            await ns.stamp(db, svc_alert_id, allowed)
            if not allowed:
                continue
            # Flap cooldown (see device path).
            if await _within_cooldown(db, rule, service_check_id=event.service_check_id,
                                      exclude_alert_id=svc_alert_id):
                await ns.stamp(db, svc_alert_id, False)
                suppressed_alerts += 1
                continue
        elif trigger != "up" and not recovered[str(rule.id)]["notified"]:
            continue

        channel_ids = rule.notify_channels or []
        delivery_alert_id = (recovered.get(str(rule.id), {}).get("alert_id")
                             if is_recovery else svc_alert_id)
        for ch_id in channel_ids:
            ch = None
            timer = dl.Timer()
            try:
                ch_result = await db.execute(
                    text("SELECT name, type, config, gateway_id, enabled FROM notification_channels WHERE id = :id"),
                    {"id": ch_id},
                )
                ch = ch_result.first()
                if not ch or not ch.enabled:
                    await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery,
                                        "skipped", "config: channel disabled or deleted")
                    continue

                ch_config = decrypt_config(ch.config)

                if ch.type == "sms":
                    phones = ch_config.get("phone_numbers", "")
                    gw_cfg = await _sms_gateway(db, ch.gateway_id or ch_config.get("gateway_id")) if phones else None
                    if not phones or not gw_cfg:
                        await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery, "skipped",
                                            "config: no phone numbers" if not phones else "config: no enabled SMS gateway")
                        continue
                    if await _send_sms(gw_cfg, phones, sms_body):
                        notifications_sent += 1
                        await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery,
                                            "accepted", None, timer.ms)
                    else:
                        await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery,
                                            "skipped", "config: SMS gateway is not a usable HTTP gateway")

                elif ch.type in ("webhook", "slack", "teams", "discord", "pagerduty"):
                    # Service alerts never reached webhook-family channels:
                    # only SMS and email were implemented on this path.
                    ctx = {
                        "hostname":     variables.get("check_name") or variables.get("hostname"),
                        "ip_address":   variables.get("target") or "",
                        "status":       (event.new_status or "").upper(),
                        "severity":     rule.severity or "warning",
                        "message":      sms_body,
                        "triggered_at": now.isoformat(),
                        "is_recovery":  is_recovery,
                        "service_check_id": event.service_check_id,
                        "rule_id":      str(rule.id),
                        "rule_name":    rule.name,
                        "alert_id":     str(delivery_alert_id) if delivery_alert_id else None,
                        "event_type":   "recovery" if is_recovery else "trigger",
                    }
                    ok, err = await _dispatch_channel_result(ch.type, ch_config, ctx)
                    if ok:
                        notifications_sent += 1
                    await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery,
                                        "accepted" if ok else ("failed" if err else "skipped"),
                                        err if err else (None if ok else "config: no URL or routing key"),
                                        timer.ms)

                elif ch.type == "email":
                    recipients = ch_config.get("recipients", "")
                    gw_conf = await _smtp_gateway(db, ch.gateway_id or ch_config.get("gateway_id")) if recipients else None
                    if not recipients or not gw_conf:
                        await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery, "skipped",
                                            "config: no recipients" if not recipients else "config: no enabled SMTP gateway")
                        continue
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
                        if await _send_email(gw_conf, recipients, email_subject,
                                             email_body, html_body=email_html):
                            notifications_sent += 1
                            await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery,
                                                "accepted", None, timer.ms)
                        else:
                            await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery,
                                                "skipped", "config: SMTP gateway has no host")

            except Exception as exc:
                print(f"ERROR sending service notification to channel {ch_id}: {exc}")
                await _log_delivery(db, delivery_alert_id, rule, ch_id, ch, is_recovery,
                                    "failed", exc, timer.ms)

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
    scope match, records the alert row, and dispatches the rule's notification
    channels (email/SMS/webhook family) with a per-rule cooldown so a trap
    storm pages once, not once per trap.
    """
    now = datetime.now(timezone.utc)

    did = event.device_id or None
    if did in ("", "00000000-0000-0000-0000-000000000000"):
        did = None

    group_id = None
    device_tags: set[str] = set()
    hostname = ""
    device_ip = ""
    if did:
        dev = (await db.execute(
            text("SELECT group_id, tags, hostname, host(ip_address) AS ip "
                 "FROM devices WHERE id = :id"), {"id": did}
        )).first()
        group_id = str(dev.group_id) if dev and dev.group_id else None
        device_tags = _tag_set(dev.tags if dev else None)
        hostname = (dev.hostname if dev else "") or ""
        device_ip = (dev.ip if dev else "") or ""

        # NCCM: a configuration-change trap schedules a debounced backup. It is
        # recorded before maintenance/dependency suppression on purpose —
        # changes made in a maintenance window are exactly what must be kept.
        try:
            from app.services import ncm_changes
            if ncm_changes.change_trap(event.trap_oid):
                async with db.begin_nested():
                    await ncm_changes.record_trap(db, did, event.trap_oid, event.event_id)
                await db.commit()
        except Exception:
            pass  # never let backup bookkeeping affect trap alerting

        # Planned downtime: traps from a device in maintenance don't raise alerts.
        if await _device_in_maintenance(db, did):
            return {"alerts_created": 0, "suppressed": "maintenance"}

        if await _find_suppressing_dependency(db, did):
            return {"alerts_created": 0, "suppressed": "dependency"}

    rules = (await db.execute(
        text("""
            SELECT id, name, severity, device_id, group_id, scope_tag, trap_oid,
                   notify_channels, cooldown, metric, operator, threshold,
                   conditions, condition_logic,
                   email_subject, email_body, sms_template,
                   schedule_start, schedule_end, schedule_days
            FROM alert_rules
            WHERE enabled = true AND metric = 'trap'
        """)
    )).fetchall()
    _tz = await get_configured_timezone(db)

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

        await db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                         {'key': f'trap:{rule.id}:{did or event.source_ip}'})
        if event.event_id:
            replayed = (await db.execute(text("SELECT 1 FROM alerts WHERE rule_id = :rid "
                "AND metadata->>'trap_event_id' = :event_id LIMIT 1"),
                {'rid': str(rule.id), 'event_id': str(event.event_id)})).first()
            if replayed:
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
        inserted = await db.execute(
            text("""
                INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, metadata)
                VALUES (:device_id, :rule_id, 'active', :severity, :message, :triggered_at, CAST(:metadata AS jsonb))
                RETURNING id
            """),
            {
                "device_id": did,
                "rule_id": str(rule.id),
                "severity": rule.severity or event.severity or "warning",
                "message": message,
                "triggered_at": now,
                "metadata": json.dumps({
                    "trap": True,
                    "trap_event_id": str(event.event_id) if event.event_id else None,
                    "notified": False,
                    "trap_oid": event.trap_oid,
                    "trap_name": event.trap_name,
                    "source_ip": event.source_ip,
                    "trap_severity": event.severity,
                }),
            },
        )
        alerts_created += 1
        alert_id = (inserted.first() or [None])[0]

        # Dispatch the rule's channels. Traps arrive in storms, so the rule's
        # cooldown (floor 60s) dedupes per rule+device: one page per burst.
        channels = rule.notify_channels or []
        if not channels:
            continue
        allowed = notifications_allowed(
            rule.schedule_start, rule.schedule_end,
            getattr(rule, "schedule_days", None), _tz,
        )
        if allowed:
            prior = (await db.execute(
                text("""
                    SELECT 1 FROM alerts
                    WHERE rule_id = :rid
                      AND device_id IS NOT DISTINCT FROM CAST(:did AS uuid)
                      AND metadata->>'source_ip' = :source_ip
                      AND id <> :aid
                      AND COALESCE(metadata->>'notified', 'true') = 'true'
                      AND COALESCE(metadata->>'trap', 'false') = 'true'
                      AND triggered_at > now() - make_interval(secs => :cd)
                    LIMIT 1
                """),
                {"rid": str(rule.id), "did": did, "aid": alert_id, "source_ip": event.source_ip,
                 "cd": max(int(rule.cooldown or 0), 60)},
            )).first()
            allowed = prior is None
        if not allowed:
            continue

        subject_name = hostname or event.source_ip
        sev = rule.severity or event.severity or "warning"
        variables = {
            "hostname": subject_name,
            "ip_address": device_ip or event.source_ip,
            "status": "TRAP",
            "severity": sev.upper(),
            "rule_name": rule.name or "Alert",
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "trap_oid": event.trap_oid or "",
            "trap_name": event.trap_name or "",
            "source_ip": event.source_ip or "",
            "trap_message": message,
            **rule_phrasing(rule, hostname=host_label(subject_name, device_ip or event.source_ip)),
        }
        trap_defaults = default_templates("trap")
        email_subject = _render(effective_template(rule.email_subject, trap_defaults["email_subject"]), variables)
        email_body = _render(effective_template(rule.email_body, trap_defaults["email_body"]), variables)
        sms_body = _render(effective_template(rule.sms_template, trap_defaults["sms_template"]), variables)

        try:
            from app.services.host_alert_service import dispatch_to_channels
            sent = await dispatch_to_channels(db, channels, {
                "subject": email_subject,
                "body": email_body,
                "message": sms_body,
                "hostname": subject_name,
                "ip_address": device_ip or event.source_ip,
                "status": "TRAP", "severity": sev, "resolved": False,
                "rule_name": rule.name,
                "details": [
                    ("Alert rule", rule.name),
                    ("Trap OID", event.trap_oid),
                    ("Trap name", event.trap_name),
                    ("Source IP", event.source_ip),
                    ("Message", event.message),
                ],
                "action_url": await _dashboard_url(db, "/alerts"),
                "triggered_at": now.isoformat(),
                "rule_id": str(rule.id),
                "is_recovery": False,
            })
            await ns.stamp(db, alert_id, bool(sent))
        except Exception as exc:
            print(f"ERROR trap notification for rule {rule.id}: {exc}")

    await db.commit()
    return {
        "matched_rules": len(rules),
        "alerts_created": alerts_created,
        "trap_oid": event.trap_oid,
    }
