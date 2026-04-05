import base64
from uuid import UUID
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User

router = APIRouter(prefix="/settings", tags=["Settings"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class SmtpConfig(BaseModel):
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    from_email: str = ""
    from_name: str = ""
    encryption: str = "tls"  # none / tls / ssl
    enabled: bool = False


class SmsConfig(BaseModel):
    provider: str = "custom_http"  # twilio / vonage / custom_http
    # Twilio/Vonage fields
    account_sid: str = ""
    auth_token: str = ""
    from_number: str = ""
    # Custom HTTP API fields
    api_url: str = ""
    http_method: str = "GET"  # GET / POST
    content_type: str = ""  # application/json, application/x-www-form-urlencoded, or empty for query params
    auth_type: str = "none"  # none / basic / bearer / query_param
    auth_username: str = ""
    auth_password: str = ""
    auth_token_value: str = ""
    # URL template with placeholders: {recipients}, {message}, {sender}
    # For GET: params appended as query string
    # For POST: body template
    request_template: str = ""
    # Custom headers as JSON string
    custom_headers: dict = {}
    # Sender name for the SMS
    sender_name: str = ""
    # Enable/disable
    enabled: bool = False


class GatewaysResponse(BaseModel):
    smtp: Optional[SmtpConfig] = None
    sms: Optional[SmsConfig] = None


class SmtpTestRequest(BaseModel):
    recipient: str


class SmsTestRequest(BaseModel):
    recipient: str


class ChannelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    type: str = Field(..., pattern="^(email|sms|webhook|slack|telegram)$")
    config: dict = Field(default_factory=dict)
    enabled: bool = True


class ChannelUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    type: Optional[str] = Field(None, pattern="^(email|sms|webhook|slack|telegram)$")
    config: Optional[dict] = None
    enabled: Optional[bool] = None


class ChannelResponse(BaseModel):
    id: str
    name: str
    type: str
    config: dict
    enabled: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_system_setting(db: AsyncSession, key: str) -> Optional[dict]:
    result = await db.execute(
        text("SELECT value FROM system_settings WHERE key = :key"),
        {"key": key},
    )
    row = result.first()
    return row[0] if row else None


async def _upsert_system_setting(db: AsyncSession, key: str, value: dict) -> None:
    await db.execute(
        text(
            "INSERT INTO system_settings (key, value) VALUES (:key, CAST(:value AS jsonb)) "
            "ON CONFLICT (key) DO UPDATE SET value = CAST(EXCLUDED.value AS jsonb)"
        ),
        {"key": key, "value": _json_dumps(value)},
    )
    await db.commit()


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj)


# ---------------------------------------------------------------------------
# Company Settings
# ---------------------------------------------------------------------------

class CompanySettings(BaseModel):
    company_name: str = ""
    company_address: str = ""
    company_email: str = ""
    company_phone: str = ""
    company_website: str = ""
    timezone: str = "UTC"
    date_format: str = "YYYY-MM-DD"
    time_format: str = "24h"


@router.get("/company")
async def get_company_settings(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    raw = await _get_system_setting(db, "company")
    if raw:
        return CompanySettings(**raw).model_dump()
    return CompanySettings().model_dump()


@router.put("/company")
async def update_company_settings(
    data: CompanySettings,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _upsert_system_setting(db, "company", data.model_dump())
    return {"message": "Company settings updated"}


@router.post("/company/logo")
async def upload_company_logo(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if file.content_type not in ("image/png", "image/jpeg", "image/svg+xml", "image/webp"):
        raise HTTPException(status_code=400, detail="Only PNG, JPEG, SVG, or WebP images are allowed")

    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Logo must be less than 2MB")

    logo_b64 = base64.b64encode(content).decode()
    await _upsert_system_setting(db, "company_logo", {
        "data": logo_b64,
        "content_type": file.content_type,
        "filename": file.filename,
    })
    return {"message": "Logo uploaded successfully"}


@router.get("/company/logo")
async def get_company_logo(
    db: AsyncSession = Depends(get_db),
):
    raw = await _get_system_setting(db, "company_logo")
    if not raw or not raw.get("data"):
        raise HTTPException(status_code=404, detail="No logo uploaded")

    content = base64.b64decode(raw["data"])
    return Response(content=content, media_type=raw.get("content_type", "image/png"))


@router.get("/timezone")
async def get_timezone(db: AsyncSession = Depends(get_db)):
    """Public endpoint for timezone (no auth required)."""
    raw = await _get_system_setting(db, "company")
    tz = raw.get("timezone", "UTC") if raw else "UTC"
    return {"timezone": tz}


def _row_to_channel(row) -> dict:
    d = {
        "id": str(row.id),
        "name": row.name,
        "type": row.type,
        "config": row.config if row.config else {},
        "enabled": row.enabled,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if hasattr(row, 'gateway_id'):
        d["gateway_id"] = str(row.gateway_id) if row.gateway_id else None
    if hasattr(row, 'gateway_name'):
        d["gateway_name"] = row.gateway_name if row.gateway_name else None
    return d


def _row_to_gateway(row) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "type": row.type,
        "config": row.config if row.config else {},
        "is_default": row.is_default,
        "enabled": row.enabled,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Legacy gateway endpoints — MUST be before parameterized {gateway_id} routes
# ---------------------------------------------------------------------------

@router.get("/gateways", response_model=GatewaysResponse)
async def get_gateways(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    smtp_raw = await _get_system_setting(db, "smtp")
    sms_raw = await _get_system_setting(db, "sms")
    return GatewaysResponse(
        smtp=SmtpConfig(**smtp_raw) if smtp_raw else SmtpConfig(),
        sms=SmsConfig(**sms_raw) if sms_raw else SmsConfig(),
    )


@router.put("/gateways/smtp")
async def update_smtp_legacy(
    data: SmtpConfig,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _upsert_system_setting(db, "smtp", data.model_dump())
    return {"message": "SMTP settings updated"}


@router.put("/gateways/sms")
async def update_sms_legacy(
    data: SmsConfig,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _upsert_system_setting(db, "sms", data.model_dump())
    return {"message": "SMS settings updated"}


@router.post("/gateways/smtp/test")
async def test_smtp_legacy(
    data: SmtpTestRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    smtp_raw = await _get_system_setting(db, "smtp")
    if not smtp_raw:
        raise HTTPException(status_code=400, detail="SMTP not configured")

    config = SmtpConfig(**smtp_raw)
    if not config.enabled:
        raise HTTPException(status_code=400, detail="SMTP gateway is disabled")
    if not config.host or not config.from_email:
        raise HTTPException(status_code=400, detail="SMTP configuration is incomplete")

    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    try:
        msg = MIMEMultipart()
        msg["From"] = f"{config.from_name} <{config.from_email}>"
        msg["To"] = data.recipient
        msg["Subject"] = "ZenPlus SMTP Test"
        msg.attach(MIMEText(
            "This is a test email from ZenPlus Monitoring System.\n\n"
            "If you received this, your SMTP configuration is working correctly.",
            "plain",
        ))

        if config.encryption == "ssl":
            server = smtplib.SMTP_SSL(config.host, config.port, timeout=10)
        else:
            server = smtplib.SMTP(config.host, config.port, timeout=10)
            if config.encryption == "tls":
                server.starttls()

        if config.username:
            server.login(config.username, config.password)

        server.sendmail(config.from_email, data.recipient, msg.as_string())
        server.quit()

        return {"message": f"Test email sent to {data.recipient}", "recipient": data.recipient}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"SMTP test failed: {str(e)}")


@router.post("/gateways/sms/test")
async def test_sms_legacy(
    data: SmsTestRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import httpx

    sms_raw = await _get_system_setting(db, "sms")
    if not sms_raw:
        raise HTTPException(status_code=400, detail="SMS not configured")

    config = SmsConfig(**sms_raw)
    if not config.enabled:
        raise HTTPException(status_code=400, detail="SMS gateway is disabled")

    if config.provider == "custom_http":
        if not config.api_url:
            raise HTTPException(status_code=400, detail="API URL is required for Custom HTTP")

        test_message = "ZenPlus Test Alert: This is a test SMS from your monitoring system."
        template = config.request_template or ""
        template = template.replace("{recipients}", data.recipient)
        template = template.replace("{message}", test_message)
        template = template.replace("{sender}", config.sender_name or "ZenPlus")
        template = template.replace("{hostname}", "test-device")
        template = template.replace("{ip_address}", "0.0.0.0")
        template = template.replace("{status}", "TEST")

        headers = dict(config.custom_headers) if config.custom_headers else {}
        auth = None
        if config.auth_type == "basic" and config.auth_username:
            auth = (config.auth_username, config.auth_password)
        elif config.auth_type == "bearer" and config.auth_token_value:
            headers["Authorization"] = f"Bearer {config.auth_token_value}"

        try:
            async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
                if config.http_method.upper() == "POST":
                    if config.content_type == "application/json":
                        try:
                            import json as json_mod
                            body = json_mod.loads(template)
                            resp = await client.post(config.api_url, json=body, headers=headers, auth=auth)
                        except (ValueError, Exception):
                            headers["Content-Type"] = config.content_type or "text/plain"
                            resp = await client.post(config.api_url, content=template, headers=headers, auth=auth)
                    elif config.content_type == "application/x-www-form-urlencoded":
                        resp = await client.post(config.api_url, content=template, headers={**headers, "Content-Type": config.content_type}, auth=auth)
                    else:
                        resp = await client.post(config.api_url, content=template, headers=headers, auth=auth)
                else:
                    url = config.api_url
                    if template:
                        sep = "&" if "?" in url else "?"
                        url = f"{url}{sep}{template}"
                    resp = await client.get(url, headers=headers, auth=auth)

                return {
                    "message": f"SMS test sent. API responded with status {resp.status_code}",
                    "status_code": resp.status_code,
                    "response": resp.text[:200],
                    "recipient": data.recipient,
                }
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"SMS send failed: {str(e)}")
    else:
        if not config.account_sid or not config.auth_token:
            raise HTTPException(status_code=400, detail="Account SID and Auth Token are required")
        return {"message": "SMS configuration is valid (Twilio/Vonage send not implemented yet)", "recipient": data.recipient}


# ---------------------------------------------------------------------------
# Gateway endpoints (multi-gateway CRUD)
# ---------------------------------------------------------------------------

@router.get("/gateways/list")
async def list_gateways(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text("SELECT id, name, type, config, is_default, enabled, created_at, updated_at FROM notification_gateways ORDER BY type, name")
    )
    return {"data": [_row_to_gateway(r) for r in result.fetchall()]}


@router.post("/gateways", status_code=201)
async def create_gateway(
    data: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    result = await db.execute(
        text(
            "INSERT INTO notification_gateways (name, type, config, is_default, enabled, created_at, updated_at) "
            "VALUES (:name, :type, CAST(:config AS jsonb), :is_default, :enabled, :created_at, :updated_at) "
            "RETURNING id, name, type, config, is_default, enabled, created_at, updated_at"
        ),
        {
            "name": data.get("name", ""),
            "type": data.get("type", "smtp"),
            "config": _json_dumps(data.get("config", {})),
            "is_default": data.get("is_default", False),
            "enabled": data.get("enabled", True),
            "created_at": now,
            "updated_at": now,
        },
    )
    await db.commit()
    row = result.first()
    return _row_to_gateway(row)


@router.put("/gateways/{gateway_id}")
async def update_gateway(
    gateway_id: UUID,
    data: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fields = {k: v for k, v in data.items() if k in ("name", "type", "config", "is_default", "enabled")}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_parts = ["updated_at = :updated_at"]
    params: dict = {"id": gateway_id, "updated_at": datetime.now(timezone.utc)}

    for key, value in fields.items():
        if key == "config":
            set_parts.append("config = CAST(:config AS jsonb)")
            params["config"] = _json_dumps(value)
        else:
            set_parts.append(f"{key} = :{key}")
            params[key] = value

    result = await db.execute(
        text(f"UPDATE notification_gateways SET {', '.join(set_parts)} WHERE id = :id "
             "RETURNING id, name, type, config, is_default, enabled, created_at, updated_at"),
        params,
    )
    await db.commit()
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Gateway not found")
    return _row_to_gateway(row)


@router.delete("/gateways/{gateway_id}", status_code=204)
async def delete_gateway(
    gateway_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text("DELETE FROM notification_gateways WHERE id = :id"), {"id": gateway_id}
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Gateway not found")


# ---------------------------------------------------------------------------
# Notification channel endpoints
# ---------------------------------------------------------------------------

@router.get("/channels")
async def list_channels(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text("SELECT c.id, c.name, c.type, c.config, c.enabled, c.created_at, c.updated_at, "
             "c.gateway_id, g.name AS gateway_name "
             "FROM notification_channels c "
             "LEFT JOIN notification_gateways g ON g.id = c.gateway_id "
             "ORDER BY c.created_at DESC")
    )
    rows = result.fetchall()
    return {"data": [_row_to_channel(r) for r in rows]}


@router.post("/channels", status_code=201)
async def create_channel(
    data: ChannelCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    gateway_id = data.config.get("gateway_id") if data.config else None
    result = await db.execute(
        text(
            "INSERT INTO notification_channels (name, type, config, enabled, gateway_id, created_at, updated_at) "
            "VALUES (:name, :type, CAST(:config AS jsonb), :enabled, :gateway_id, :created_at, :updated_at) "
            "RETURNING id, name, type, config, enabled, gateway_id, created_at, updated_at"
        ),
        {
            "name": data.name,
            "type": data.type,
            "config": _json_dumps(data.config),
            "enabled": data.enabled,
            "gateway_id": gateway_id,
            "created_at": now,
            "updated_at": now,
        },
    )
    await db.commit()
    row = result.first()
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create channel")
    return _row_to_channel(row)


@router.put("/channels/{channel_id}")
async def update_channel(
    channel_id: UUID,
    data: ChannelUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Build dynamic SET clause from provided fields
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_parts = []
    params: dict = {"id": channel_id, "updated_at": datetime.now(timezone.utc)}

    for key, value in fields.items():
        if key == "config":
            set_parts.append("config = CAST(:config AS jsonb)")
            params["config"] = _json_dumps(value)
        else:
            set_parts.append(f"{key} = :{key}")
            params[key] = value

    set_parts.append("updated_at = :updated_at")
    set_clause = ", ".join(set_parts)

    result = await db.execute(
        text(
            f"UPDATE notification_channels SET {set_clause} "
            "WHERE id = :id "
            "RETURNING id, name, type, config, enabled, created_at, updated_at"
        ),
        params,
    )
    await db.commit()
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Channel not found")
    return _row_to_channel(row)


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text("DELETE FROM notification_channels WHERE id = :id RETURNING id"),
        {"id": channel_id},
    )
    await db.commit()
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Channel not found")


@router.post("/channels/{channel_id}/test")
async def test_channel(
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import httpx
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    # Get channel
    result = await db.execute(
        text("SELECT id, name, type, config, enabled, gateway_id FROM notification_channels WHERE id = :id"),
        {"id": channel_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Channel not found")
    if not row.enabled:
        raise HTTPException(status_code=400, detail="Channel is disabled")

    config = row.config or {}
    channel_type = row.type
    test_message = "ZenPlus Test: This is a test notification from your monitoring system."

    # ─── SMS Channel ───
    if channel_type == "sms":
        phone_numbers = config.get("phone_numbers", "")
        if not phone_numbers:
            raise HTTPException(status_code=400, detail="SMS channel has no phone numbers configured")

        # Find the gateway (linked or default)
        gw_id = row.gateway_id or config.get("gateway_id")
        if gw_id:
            gw_result = await db.execute(text("SELECT config FROM notification_gateways WHERE id = :id"), {"id": gw_id})
        else:
            gw_result = await db.execute(text("SELECT config FROM notification_gateways WHERE type = 'sms' AND is_default = true LIMIT 1"))
        gw_row = gw_result.first()

        if not gw_row:
            # Fallback to system_settings
            gw_config = await _get_system_setting(db, "sms")
            if not gw_config:
                raise HTTPException(status_code=400, detail="No SMS gateway configured")
        else:
            gw_config = gw_row.config

        sms_cfg = SmsConfig(**gw_config)
        if not sms_cfg.enabled:
            raise HTTPException(status_code=400, detail="SMS gateway is disabled")

        if sms_cfg.provider == "custom_http":
            if not sms_cfg.api_url:
                raise HTTPException(status_code=400, detail="SMS gateway API URL not configured")

            template = sms_cfg.request_template or ""
            template = template.replace("{recipients}", phone_numbers)
            template = template.replace("{message}", test_message)
            template = template.replace("{sender}", sms_cfg.sender_name or "ZenPlus")
            template = template.replace("{hostname}", "test-device")
            template = template.replace("{ip_address}", "0.0.0.0")
            template = template.replace("{status}", "TEST")

            headers = dict(sms_cfg.custom_headers) if sms_cfg.custom_headers else {}
            auth = None
            if sms_cfg.auth_type == "basic" and sms_cfg.auth_username:
                auth = (sms_cfg.auth_username, sms_cfg.auth_password)
            elif sms_cfg.auth_type == "bearer" and sms_cfg.auth_token_value:
                headers["Authorization"] = f"Bearer {sms_cfg.auth_token_value}"

            try:
                async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
                    if sms_cfg.http_method.upper() == "POST":
                        if sms_cfg.content_type == "application/json":
                            try:
                                import json as json_mod
                                body = json_mod.loads(template)
                                resp = await client.post(sms_cfg.api_url, json=body, headers=headers, auth=auth)
                            except (ValueError,):
                                resp = await client.post(sms_cfg.api_url, content=template, headers=headers, auth=auth)
                        else:
                            resp = await client.post(sms_cfg.api_url, content=template, headers=headers, auth=auth)
                    else:
                        url = sms_cfg.api_url
                        if template:
                            sep = "&" if "?" in url else "?"
                            url = f"{url}{sep}{template}"
                        resp = await client.get(url, headers=headers, auth=auth)

                return {
                    "message": f"Test SMS sent to {phone_numbers}. API status: {resp.status_code}",
                    "status_code": resp.status_code,
                    "response": resp.text[:300],
                }
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"SMS send failed: {str(e)}")
        else:
            return {"message": f"SMS test via {sms_cfg.provider} not implemented yet. Numbers: {phone_numbers}"}

    # ─── Email Channel ───
    elif channel_type == "email":
        recipients = config.get("recipients", "")
        if not recipients:
            raise HTTPException(status_code=400, detail="Email channel has no recipients configured")

        # Find SMTP gateway
        gw_id = row.gateway_id or config.get("gateway_id")
        if gw_id:
            gw_result = await db.execute(text("SELECT config FROM notification_gateways WHERE id = :id"), {"id": gw_id})
        else:
            gw_result = await db.execute(text("SELECT config FROM notification_gateways WHERE type = 'smtp' AND is_default = true LIMIT 1"))
        gw_row = gw_result.first()

        if not gw_row:
            gw_config = await _get_system_setting(db, "smtp")
            if not gw_config:
                raise HTTPException(status_code=400, detail="No SMTP gateway configured")
        else:
            gw_config = gw_row.config

        smtp_cfg = SmtpConfig(**gw_config)
        if not smtp_cfg.enabled:
            raise HTTPException(status_code=400, detail="SMTP gateway is disabled")
        if not smtp_cfg.host:
            raise HTTPException(status_code=400, detail="SMTP gateway host not configured")

        recipient_list = [r.strip() for r in recipients.split(",") if r.strip()]
        try:
            msg = MIMEMultipart()
            msg["From"] = f"{smtp_cfg.from_name} <{smtp_cfg.from_email}>"
            msg["To"] = ", ".join(recipient_list)
            msg["Subject"] = "ZenPlus Test Notification"
            msg.attach(MIMEText(
                f"This is a test email from ZenPlus Monitoring System.\n\n"
                f"Channel: {row.name}\n"
                f"Recipients: {recipients}\n\n"
                f"If you received this, your email notification channel is working correctly.",
                "plain",
            ))

            if smtp_cfg.encryption == "ssl":
                server = smtplib.SMTP_SSL(smtp_cfg.host, smtp_cfg.port, timeout=10)
            else:
                server = smtplib.SMTP(smtp_cfg.host, smtp_cfg.port, timeout=10)
                if smtp_cfg.encryption == "tls":
                    server.starttls()
            if smtp_cfg.username:
                server.login(smtp_cfg.username, smtp_cfg.password)
            server.sendmail(smtp_cfg.from_email, recipient_list, msg.as_string())
            server.quit()
            return {"message": f"Test email sent to {recipients}"}
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Email send failed: {str(e)}")

    # ─── Webhook ───
    elif channel_type == "webhook":
        url = config.get("url", "")
        if not url:
            raise HTTPException(status_code=400, detail="Webhook URL not configured")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json={"text": test_message, "source": "zenplus_test"})
            return {"message": f"Webhook sent. Status: {resp.status_code}"}
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Webhook failed: {str(e)}")

    # ─── Slack ───
    elif channel_type == "slack":
        webhook_url = config.get("webhook_url", "")
        if not webhook_url:
            raise HTTPException(status_code=400, detail="Slack webhook URL not configured")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json={"text": test_message})
            return {"message": f"Slack message sent. Status: {resp.status_code}"}
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Slack failed: {str(e)}")

    # ─── Telegram ───
    elif channel_type == "telegram":
        bot_token = config.get("bot_token", "")
        chat_id = config.get("chat_id", "")
        if not bot_token or not chat_id:
            raise HTTPException(status_code=400, detail="Telegram bot_token or chat_id missing")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": test_message},
                )
            return {"message": f"Telegram message sent. Status: {resp.status_code}"}
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Telegram failed: {str(e)}")

    else:
        raise HTTPException(status_code=400, detail=f"Unknown channel type: {channel_type}")
