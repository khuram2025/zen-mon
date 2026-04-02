import json
from uuid import UUID
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User

router = APIRouter(prefix="/alert-rules", tags=["Alert Rules"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class AlertRuleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: bool = True

    metric: str = Field(..., pattern="^(ping_status|rtt|packet_loss|jitter)$")
    operator: str = Field(..., pattern="^(>|<|>=|<=|==|!=)$")
    threshold: float

    trigger_on: str = Field(default="any", pattern="^(any|down|up|degraded)$")
    recovery_alert: bool = False

    device_id: Optional[UUID] = None
    group_id: Optional[UUID] = None
    device_type: Optional[str] = None
    location: Optional[str] = None

    severity: str = Field(default="warning", pattern="^(info|warning|critical)$")
    notify_channels: list[str] = Field(default_factory=list)
    cooldown: int = Field(default=300, ge=0)
    min_duration: int = Field(default=0, ge=0)
    max_repeat: int = Field(default=0, ge=0)

    schedule_start: Optional[str] = None  # HH:MM
    schedule_end: Optional[str] = None    # HH:MM
    schedule_days: Optional[list[int]] = None  # 1-7 (Mon-Sun)

    # Message templates
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    sms_template: Optional[str] = None
    recovery_email_subject: Optional[str] = None
    recovery_email_body: Optional[str] = None
    recovery_sms_template: Optional[str] = None


class AlertRuleUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: Optional[bool] = None

    metric: Optional[str] = Field(None, pattern="^(ping_status|rtt|packet_loss|jitter)$")
    operator: Optional[str] = Field(None, pattern="^(>|<|>=|<=|==|!=|eq|neq|gt|lt|gte|lte)$")
    threshold: Optional[float] = None

    trigger_on: Optional[str] = Field(None, pattern="^(any|down|up|degraded)$")
    recovery_alert: Optional[bool] = None

    device_id: Optional[UUID] = None
    group_id: Optional[UUID] = None
    device_type: Optional[str] = None
    location: Optional[str] = None

    severity: Optional[str] = Field(None, pattern="^(info|warning|critical)$")
    notify_channels: Optional[list[str]] = None
    cooldown: Optional[int] = Field(None, ge=0)
    min_duration: Optional[int] = Field(None, ge=0)
    max_repeat: Optional[int] = Field(None, ge=0)

    schedule_start: Optional[str] = None
    schedule_end: Optional[str] = None
    schedule_days: Optional[list[int]] = None

    # Message templates
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    sms_template: Optional[str] = None
    recovery_email_subject: Optional[str] = None
    recovery_email_body: Optional[str] = None
    recovery_sms_template: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Columns to SELECT in list / get queries
_RULE_COLUMNS = (
    "id, name, description, enabled, metric, operator, threshold, duration, "
    "device_id, group_id, severity, notify_channels, cooldown, "
    "device_type, location, trigger_on, recovery_alert, "
    "min_duration, max_repeat, schedule_start, schedule_end, schedule_days, "
    "email_subject, email_body, sms_template, "
    "recovery_email_subject, recovery_email_body, recovery_sms_template, "
    "created_at, updated_at, created_by"
)


def _row_to_dict(row) -> dict:
    schedule_start = row.schedule_start
    schedule_end = row.schedule_end
    if schedule_start is not None and not isinstance(schedule_start, str):
        schedule_start = schedule_start.strftime("%H:%M")
    if schedule_end is not None and not isinstance(schedule_end, str):
        schedule_end = schedule_end.strftime("%H:%M")

    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "enabled": row.enabled,
        "metric": row.metric,
        "operator": row.operator,
        "threshold": float(row.threshold) if row.threshold is not None else None,
        "duration": row.duration,
        "device_id": str(row.device_id) if row.device_id else None,
        "group_id": str(row.group_id) if row.group_id else None,
        "severity": row.severity,
        "notify_channels": row.notify_channels if row.notify_channels else [],
        "cooldown": row.cooldown,
        "device_type": row.device_type,
        "location": row.location,
        "trigger_on": row.trigger_on,
        "recovery_alert": row.recovery_alert,
        "min_duration": row.min_duration,
        "max_repeat": row.max_repeat,
        "schedule_start": schedule_start,
        "schedule_end": schedule_end,
        "schedule_days": row.schedule_days if row.schedule_days else [],
        "email_subject": row.email_subject,
        "email_body": row.email_body,
        "sms_template": row.sms_template,
        "recovery_email_subject": row.recovery_email_subject,
        "recovery_email_body": row.recovery_email_body,
        "recovery_sms_template": row.recovery_sms_template,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "created_by": str(row.created_by) if row.created_by else None,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def list_alert_rules(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text(f"SELECT {_RULE_COLUMNS} FROM alert_rules ORDER BY created_at DESC")
    )
    rows = result.fetchall()
    return {"data": [_row_to_dict(r) for r in rows]}


@router.post("", status_code=201)
async def create_alert_rule(
    data: AlertRuleCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    params: dict = {
        "name": data.name,
        "description": data.description,
        "enabled": data.enabled,
        "metric": data.metric,
        "operator": data.operator,
        "threshold": data.threshold,
        "duration": data.min_duration,  # legacy column maps to min_duration
        "device_id": data.device_id,
        "group_id": data.group_id,
        "severity": data.severity,
        "notify_channels": json.dumps(data.notify_channels),
        "cooldown": data.cooldown,
        "device_type": data.device_type,
        "location": data.location,
        "trigger_on": data.trigger_on,
        "recovery_alert": data.recovery_alert,
        "min_duration": data.min_duration,
        "max_repeat": data.max_repeat,
        "schedule_start": data.schedule_start,
        "schedule_end": data.schedule_end,
        "schedule_days": json.dumps(data.schedule_days) if data.schedule_days else None,
        "email_subject": getattr(data, 'email_subject', None),
        "email_body": getattr(data, 'email_body', None),
        "sms_template": getattr(data, 'sms_template', None),
        "recovery_email_subject": getattr(data, 'recovery_email_subject', None),
        "recovery_email_body": getattr(data, 'recovery_email_body', None),
        "recovery_sms_template": getattr(data, 'recovery_sms_template', None),
        "created_at": now,
        "updated_at": now,
        "created_by": user.id,
    }

    result = await db.execute(
        text(
            "INSERT INTO alert_rules "
            "(name, description, enabled, metric, operator, threshold, duration, "
            "device_id, group_id, severity, notify_channels, cooldown, "
            "device_type, location, trigger_on, recovery_alert, "
            "min_duration, max_repeat, schedule_start, schedule_end, schedule_days, "
            "email_subject, email_body, sms_template, "
            "recovery_email_subject, recovery_email_body, recovery_sms_template, "
            "created_at, updated_at, created_by) "
            "VALUES "
            "(:name, :description, :enabled, :metric, :operator, :threshold, :duration, "
            ":device_id, :group_id, :severity, CAST(:notify_channels AS jsonb), :cooldown, "
            ":device_type, :location, :trigger_on, :recovery_alert, "
            ":min_duration, :max_repeat, CAST(:schedule_start AS time), CAST(:schedule_end AS time), "
            "CAST(:schedule_days AS jsonb), "
            ":email_subject, :email_body, :sms_template, "
            ":recovery_email_subject, :recovery_email_body, :recovery_sms_template, "
            ":created_at, :updated_at, :created_by) "
            f"RETURNING {_RULE_COLUMNS}"
        ),
        params,
    )
    await db.commit()
    row = result.first()
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create alert rule")
    return _row_to_dict(row)


@router.get("/{rule_id}")
async def get_alert_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text(f"SELECT {_RULE_COLUMNS} FROM alert_rules WHERE id = :id"),
        {"id": rule_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return _row_to_dict(row)


@router.put("/{rule_id}")
async def update_alert_rule(
    rule_id: UUID,
    data: AlertRuleUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_parts = []
    params: dict = {"id": rule_id, "updated_at": datetime.now(timezone.utc)}

    # Special-case columns that need casts
    _jsonb_cols = {"notify_channels", "schedule_days"}
    _time_cols = {"schedule_start", "schedule_end"}

    for key, value in fields.items():
        if key in _jsonb_cols:
            set_parts.append(f"{key} = CAST(:{key} AS jsonb)")
            params[key] = json.dumps(value) if value is not None else None
        elif key in _time_cols:
            set_parts.append(f"{key} = CAST(:{key} AS time)")
            params[key] = value
        elif key == "device_id" or key == "group_id":
            set_parts.append(f"{key} = :{key}")
            params[key] = value
        else:
            set_parts.append(f"{key} = :{key}")
            params[key] = value

    # Keep legacy duration column in sync with min_duration
    if "min_duration" in fields:
        set_parts.append("duration = :min_duration_dup")
        params["min_duration_dup"] = fields["min_duration"]

    set_parts.append("updated_at = :updated_at")
    set_clause = ", ".join(set_parts)

    result = await db.execute(
        text(
            f"UPDATE alert_rules SET {set_clause} "
            f"WHERE id = :id "
            f"RETURNING {_RULE_COLUMNS}"
        ),
        params,
    )
    await db.commit()
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return _row_to_dict(row)


@router.delete("/{rule_id}", status_code=204)
async def delete_alert_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text("DELETE FROM alert_rules WHERE id = :id RETURNING id"),
        {"id": rule_id},
    )
    await db.commit()
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")


@router.post("/{rule_id}/toggle")
async def toggle_alert_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text(
            "UPDATE alert_rules SET enabled = NOT enabled, updated_at = :now "
            "WHERE id = :id "
            "RETURNING id, name, enabled"
        ),
        {"id": rule_id, "now": datetime.now(timezone.utc)},
    )
    await db.commit()
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return {
        "id": str(row.id),
        "name": row.name,
        "enabled": row.enabled,
        "message": f"Rule '{ row.name }' {'enabled' if row.enabled else 'disabled'}",
    }


def _render_template(template: str, variables: dict) -> str:
    """Replace {var_name} placeholders with values."""
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


def _build_alert_message(rule_dict: dict, is_recovery: bool = False) -> dict:
    """Build a preview/simulation alert message from a rule."""
    trigger = rule_dict.get("trigger_on", "any")
    if is_recovery:
        device_status = "UP"
        status_intro = "The following alert has been resolved:"
    else:
        device_status = "DOWN" if trigger == "down" else "DEGRADED" if trigger == "degraded" else "UP" if trigger == "up" else "ALERT"
        status_intro = "An alert has been triggered:"

    # Template variables available to users
    variables = {
        "severity": rule_dict.get("severity", "warning").upper(),
        "rule_name": rule_dict.get("name", "Alert Rule"),
        "hostname": "core-router-01",
        "ip_address": "10.0.0.1",
        "status": device_status,
        "status_intro": status_intro,
        "device_type": rule_dict.get("device_type") or "router",
        "group": "Core Network",
        "location": rule_dict.get("location") or "DC-1 Rack A1",
        "metric": rule_dict.get("metric", "ping_status"),
        "operator": rule_dict.get("operator", "=="),
        "threshold": str(rule_dict.get("threshold", 0)),
        "timestamp": "2026-04-02 12:00:00 UTC",
        "duration": "3m 25s",
        "rtt": "45.2ms",
        "packet_loss": "15%",
    }

    # Get templates from rule or use defaults
    if is_recovery:
        subject_tpl = rule_dict.get("recovery_email_subject") or "[{severity}] RESOLVED: {rule_name}"
        body_tpl = rule_dict.get("recovery_email_body") or rule_dict.get("email_body") or (
            "{status_intro}\n\n"
            "Rule: {rule_name}\nSeverity: {severity}\n"
            "Device: {hostname} ({ip_address})\nStatus: {status}\n"
            "Group: {group}\nLocation: {location}\nType: {device_type}\n"
            "Metric: {metric} {operator} {threshold}\nTime: {timestamp}\n\n"
            "--\nZenPlus Network Monitoring System"
        )
        sms_tpl = rule_dict.get("recovery_sms_template") or "[ZenPlus {severity}] {hostname} ({ip_address}) is {status}. RESOLVED: {rule_name}"
    else:
        subject_tpl = rule_dict.get("email_subject") or "[{severity}] {status}: {rule_name}"
        body_tpl = rule_dict.get("email_body") or (
            "{status_intro}\n\n"
            "Rule: {rule_name}\nSeverity: {severity}\n"
            "Device: {hostname} ({ip_address})\nStatus: {status}\n"
            "Group: {group}\nLocation: {location}\nType: {device_type}\n"
            "Metric: {metric} {operator} {threshold}\nTime: {timestamp}\n\n"
            "--\nZenPlus Network Monitoring System"
        )
        sms_tpl = rule_dict.get("sms_template") or "[ZenPlus {severity}] {hostname} ({ip_address}) is {status}. Rule: {rule_name}"

    return {
        "subject": _render_template(subject_tpl, variables),
        "email_body": _render_template(body_tpl, variables),
        "sms_body": _render_template(sms_tpl, variables),
        "sim_hostname": variables["hostname"],
        "sim_ip": variables["ip_address"],
        "device_status": device_status,
        "severity": variables["severity"],
        # Return raw templates for editing
        "email_subject_template": subject_tpl,
        "email_body_template": body_tpl,
        "sms_template": sms_tpl,
    }


@router.post("/{rule_id}/preview")
async def preview_alert_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text(f"SELECT {_RULE_COLUMNS} FROM alert_rules WHERE id = :id"),
        {"id": rule_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")

    rule = _row_to_dict(row)
    alert_msg = _build_alert_message(rule, is_recovery=False)
    recovery_msg = _build_alert_message(rule, is_recovery=True) if rule.get("recovery_alert") else None

    return {
        "alert": alert_msg,
        "recovery": recovery_msg,
    }


@router.post("/{rule_id}/simulate")
async def simulate_alert_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import httpx
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    # Get rule
    result = await db.execute(
        text(f"SELECT {_RULE_COLUMNS} FROM alert_rules WHERE id = :id"),
        {"id": rule_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Alert rule not found")

    rule = _row_to_dict(row)
    alert_msg = _build_alert_message(rule)
    notify_channels = rule.get("notify_channels", [])
    results = []

    if not notify_channels:
        return {"message": "No notification channels configured for this rule", "results": []}

    for ch_id in notify_channels:
        ch_result = await db.execute(
            text("SELECT id, name, type, config, enabled, gateway_id FROM notification_channels WHERE id = :id"),
            {"id": ch_id},
        )
        ch_row = ch_result.first()
        if not ch_row:
            results.append({"channel": ch_id, "status": "error", "detail": "Channel not found"})
            continue
        if not ch_row.enabled:
            results.append({"channel": ch_row.name, "status": "skipped", "detail": "Channel disabled"})
            continue

        ch_config = ch_row.config or {}

        try:
            if ch_row.type == "sms":
                phones = ch_config.get("phone_numbers", "")
                if not phones:
                    results.append({"channel": ch_row.name, "status": "error", "detail": "No phone numbers"})
                    continue

                # Get SMS gateway
                gw_id = ch_row.gateway_id or ch_config.get("gateway_id")
                if gw_id:
                    gw_res = await db.execute(text("SELECT config FROM notification_gateways WHERE id = :id"), {"id": gw_id})
                else:
                    gw_res = await db.execute(text("SELECT config FROM notification_gateways WHERE type = 'sms' AND is_default = true LIMIT 1"))
                gw_row = gw_res.first()
                if not gw_row:
                    gw_raw = await db.execute(text("SELECT value FROM system_settings WHERE key = 'sms'"))
                    gw_row2 = gw_raw.first()
                    gw_cfg = gw_row2[0] if gw_row2 else None
                else:
                    gw_cfg = gw_row.config

                if not gw_cfg:
                    results.append({"channel": ch_row.name, "status": "error", "detail": "No SMS gateway"})
                    continue

                if gw_cfg.get("provider") == "custom_http" and gw_cfg.get("api_url"):
                    template = gw_cfg.get("request_template", "")
                    template = template.replace("{recipients}", phones)
                    template = template.replace("{message}", alert_msg["sms_body"])
                    template = template.replace("{sender}", gw_cfg.get("sender_name", "ZenPlus"))
                    template = template.replace("{hostname}", alert_msg["sim_hostname"])
                    template = template.replace("{ip_address}", alert_msg["sim_ip"])
                    template = template.replace("{status}", alert_msg["device_status"])

                    headers = dict(gw_cfg.get("custom_headers", {}))
                    auth = None
                    if gw_cfg.get("auth_type") == "basic":
                        auth = (gw_cfg.get("auth_username", ""), gw_cfg.get("auth_password", ""))

                    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
                        if gw_cfg.get("http_method", "GET").upper() == "POST":
                            resp = await client.post(gw_cfg["api_url"], content=template, headers=headers, auth=auth)
                        else:
                            url = gw_cfg["api_url"]
                            sep = "&" if "?" in url else "?"
                            url = f"{url}{sep}{template}" if template else url
                            resp = await client.get(url, headers=headers, auth=auth)

                    results.append({"channel": ch_row.name, "type": "sms", "status": "sent", "detail": f"Status {resp.status_code}", "recipients": phones})
                else:
                    results.append({"channel": ch_row.name, "type": "sms", "status": "skipped", "detail": f"Provider {gw_cfg.get('provider')} not supported for simulation"})

            elif ch_row.type == "email":
                recipients = ch_config.get("recipients", "")
                if not recipients:
                    results.append({"channel": ch_row.name, "status": "error", "detail": "No recipients"})
                    continue

                gw_id = ch_row.gateway_id or ch_config.get("gateway_id")
                if gw_id:
                    gw_res = await db.execute(text("SELECT config FROM notification_gateways WHERE id = :id"), {"id": gw_id})
                else:
                    gw_res = await db.execute(text("SELECT config FROM notification_gateways WHERE type = 'smtp' AND is_default = true LIMIT 1"))
                gw_row = gw_res.first()
                if not gw_row:
                    gw_raw = await db.execute(text("SELECT value FROM system_settings WHERE key = 'smtp'"))
                    gw_row2 = gw_raw.first()
                    gw_cfg = gw_row2[0] if gw_row2 else None
                else:
                    gw_cfg = gw_row.config

                if not gw_cfg or not gw_cfg.get("host"):
                    results.append({"channel": ch_row.name, "status": "error", "detail": "No SMTP gateway configured"})
                    continue

                recipient_list = [r.strip() for r in recipients.split(",") if r.strip()]
                msg = MIMEMultipart()
                msg["From"] = f"{gw_cfg.get('from_name', 'ZenPlus')} <{gw_cfg.get('from_email', '')}>"
                msg["To"] = ", ".join(recipient_list)
                msg["Subject"] = f"[SIMULATION] {alert_msg['subject']}"
                msg.attach(MIMEText(f"*** THIS IS A SIMULATION ***\n\n{alert_msg['email_body']}", "plain"))

                enc = gw_cfg.get("encryption", "tls")
                if enc == "ssl":
                    server = smtplib.SMTP_SSL(gw_cfg["host"], gw_cfg.get("port", 465), timeout=10)
                else:
                    server = smtplib.SMTP(gw_cfg["host"], gw_cfg.get("port", 587), timeout=10)
                    if enc == "tls":
                        server.starttls()
                if gw_cfg.get("username"):
                    server.login(gw_cfg["username"], gw_cfg.get("password", ""))
                server.sendmail(gw_cfg.get("from_email", ""), recipient_list, msg.as_string())
                server.quit()
                results.append({"channel": ch_row.name, "type": "email", "status": "sent", "detail": f"Sent to {recipients}"})

            else:
                results.append({"channel": ch_row.name, "type": ch_row.type, "status": "skipped", "detail": "Simulation not supported for this type"})

        except Exception as e:
            results.append({"channel": ch_row.name, "status": "error", "detail": str(e)[:200]})

    sent = sum(1 for r in results if r["status"] == "sent")
    return {
        "message": f"Simulation complete: {sent}/{len(results)} notifications sent",
        "alert_preview": alert_msg,
        "results": results,
    }
