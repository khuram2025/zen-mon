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


async def _send_email(gw_config: dict, recipients: str, subject: str, body: str):
    """Send email via SMTP gateway."""
    if not gw_config.get("host"):
        return

    recipient_list = [r.strip() for r in recipients.split(",") if r.strip()]
    msg = MIMEMultipart()
    msg["From"] = f"{gw_config.get('from_name', 'ZenPlus')} <{gw_config.get('from_email', '')}>"
    msg["To"] = ", ".join(recipient_list)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

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
        text("SELECT device_type, group_id, location FROM devices WHERE id = :id"),
        {"id": event.device_id},
    )
    dev_row = dev_result.first()
    device_type = dev_row.device_type if dev_row else event.device_type
    group_id = str(dev_row.group_id) if dev_row and dev_row.group_id else event.group_id
    location = dev_row.location if dev_row else event.location

    # Get group name
    group_name = ""
    if group_id:
        gr = await db.execute(text("SELECT name FROM device_groups WHERE id = :id"), {"id": group_id})
        gr_row = gr.first()
        group_name = gr_row.name if gr_row else ""

    # Fetch all enabled alert rules
    rules_result = await db.execute(
        text("""
            SELECT id, name, trigger_on, recovery_alert, severity,
                   device_id, group_id, device_type, location,
                   notify_channels, cooldown,
                   email_subject, email_body, sms_template,
                   recovery_email_subject, recovery_email_body, recovery_sms_template
            FROM alert_rules
            WHERE enabled = true
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
        "rtt": f"{event.rtt_ms:.1f}ms",
        "packet_loss": f"{event.packet_loss:.0%}",
        "status_intro": "The following alert has been resolved:" if is_recovery else "An alert has been triggered:",
    }

    notifications_sent = 0

    for rule in rules:
        # Check trigger_on match
        trigger = rule.trigger_on or "any"
        if trigger == "down" and not is_down:
            continue
        if trigger == "up" and event.new_status != "up":
            continue
        if trigger == "degraded" and event.new_status != "degraded":
            continue
        # "any" matches everything

        # Check if this is a recovery and rule has recovery_alert
        if is_recovery and not rule.recovery_alert:
            continue
        if is_recovery and trigger != "any" and trigger != "up":
            # Only fire recovery if trigger is 'any' or 'up'
            pass

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

        # Rule matches! Send notifications
        variables["severity"] = (rule.severity or "warning").upper()
        variables["rule_name"] = rule.name or "Alert"

        # Build messages from templates
        if is_recovery:
            email_subject = _render(rule.recovery_email_subject or "[{severity}] RESOLVED: {rule_name}", variables)
            email_body = _render(rule.recovery_email_body or rule.email_body or variables["status_intro"], variables)
            sms_body = _render(rule.recovery_sms_template or "[ZenPlus {severity}] {hostname} is {status}. RESOLVED: {rule_name}", variables)
        else:
            email_subject = _render(rule.email_subject or "[{severity}] {status}: {rule_name}", variables)
            email_body = _render(rule.email_body or variables["status_intro"], variables)
            sms_body = _render(rule.sms_template or "[ZenPlus {severity}] {hostname} ({ip_address}) is {status}. Rule: {rule_name}", variables)

        # Create alert record in DB
        await db.execute(
            text("""
                INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, metadata)
                VALUES (:device_id, :rule_id, 'active', :severity, :message, :triggered_at, CAST(:metadata AS jsonb))
            """),
            {
                "device_id": event.device_id,
                "rule_id": str(rule.id),
                "severity": rule.severity or "warning",
                "message": sms_body,
                "triggered_at": now,
                "metadata": json.dumps({"old_status": event.old_status, "new_status": event.new_status, "is_recovery": is_recovery}),
            },
        )

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

                    gw_id = ch.gateway_id or ch_config.get("gateway_id")
                    if gw_id:
                        gw_res = await db.execute(text("SELECT config FROM notification_gateways WHERE id = :id"), {"id": gw_id})
                    else:
                        gw_res = await db.execute(text("SELECT config FROM notification_gateways WHERE type = 'smtp' AND is_default = true LIMIT 1"))
                    gw_row = gw_res.first()
                    if gw_row:
                        await _send_email(dict(gw_row.config), recipients, email_subject, email_body)
                        notifications_sent += 1

            except Exception as exc:
                # Log but don't fail the whole evaluation
                print(f"ERROR sending notification to channel {ch_id}: {exc}")

    await db.commit()

    return {
        "evaluated_rules": len(rules),
        "notifications_sent": notifications_sent,
        "device": event.hostname,
        "old_status": event.old_status,
        "new_status": event.new_status,
    }
