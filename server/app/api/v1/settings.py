from uuid import UUID
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
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


def _row_to_channel(row) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "type": row.type,
        "config": row.config if row.config else {},
        "enabled": row.enabled,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Gateway endpoints
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
async def update_smtp(
    data: SmtpConfig,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _upsert_system_setting(db, "smtp", data.model_dump())
    return {"message": "SMTP settings updated"}


@router.put("/gateways/sms")
async def update_sms(
    data: SmsConfig,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _upsert_system_setting(db, "sms", data.model_dump())
    return {"message": "SMS settings updated"}


@router.post("/gateways/smtp/test")
async def test_smtp(
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

    # Validate config is present; actual sending would be done by a background service.
    return {"message": "SMTP configuration is valid", "recipient": data.recipient}


@router.post("/gateways/sms/test")
async def test_sms(
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

        # Build the request from template
        test_message = "ZenPlus Test Alert: This is a test SMS from your monitoring system."
        template = config.request_template or ""
        template = template.replace("{recipients}", data.recipient)
        template = template.replace("{message}", test_message)
        template = template.replace("{sender}", config.sender_name or "ZenPlus")
        template = template.replace("{hostname}", "test-device")
        template = template.replace("{ip_address}", "0.0.0.0")
        template = template.replace("{status}", "TEST")

        # Build headers
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
                        # Try to parse template as JSON, fallback to raw
                        try:
                            import json as json_mod
                            body = json_mod.loads(template)
                            resp = await client.post(config.api_url, json=body, headers=headers, auth=auth)
                        except (json_mod.JSONDecodeError, ValueError):
                            headers["Content-Type"] = config.content_type or "text/plain"
                            resp = await client.post(config.api_url, content=template, headers=headers, auth=auth)
                    elif config.content_type == "application/x-www-form-urlencoded":
                        resp = await client.post(config.api_url, content=template, headers={**headers, "Content-Type": config.content_type}, auth=auth)
                    else:
                        resp = await client.post(config.api_url, content=template, headers=headers, auth=auth)
                else:
                    # GET - append template as query string
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
        # Twilio/Vonage - validate config
        if not config.account_sid or not config.auth_token:
            raise HTTPException(status_code=400, detail="Account SID and Auth Token are required")
        return {"message": "SMS configuration is valid (Twilio/Vonage send not implemented yet)", "recipient": data.recipient}


# ---------------------------------------------------------------------------
# Notification channel endpoints
# ---------------------------------------------------------------------------

@router.get("/channels")
async def list_channels(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        text("SELECT id, name, type, config, enabled, created_at, updated_at "
             "FROM notification_channels ORDER BY created_at DESC")
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
    result = await db.execute(
        text(
            "INSERT INTO notification_channels (name, type, config, enabled, created_at, updated_at) "
            "VALUES (:name, :type, CAST(:config AS jsonb), :enabled, :created_at, :updated_at) "
            "RETURNING id, name, type, config, enabled, created_at, updated_at"
        ),
        {
            "name": data.name,
            "type": data.type,
            "config": _json_dumps(data.config),
            "enabled": data.enabled,
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
    result = await db.execute(
        text("SELECT id, name, type, config, enabled FROM notification_channels WHERE id = :id"),
        {"id": channel_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Channel not found")
    if not row.enabled:
        raise HTTPException(status_code=400, detail="Channel is disabled")

    # Validate channel configuration based on type
    config = row.config or {}
    channel_type = row.type

    if channel_type == "email" and not config.get("recipients"):
        raise HTTPException(status_code=400, detail="Email channel has no recipients configured")
    if channel_type == "sms" and not config.get("phone_numbers"):
        raise HTTPException(status_code=400, detail="SMS channel has no phone numbers configured")
    if channel_type == "webhook" and not config.get("url"):
        raise HTTPException(status_code=400, detail="Webhook channel has no URL configured")
    if channel_type == "slack" and not config.get("webhook_url"):
        raise HTTPException(status_code=400, detail="Slack channel has no webhook URL configured")
    if channel_type == "telegram" and (not config.get("bot_token") or not config.get("chat_id")):
        raise HTTPException(status_code=400, detail="Telegram channel is missing bot_token or chat_id")

    return {
        "message": f"Channel '{row.name}' configuration is valid",
        "channel_id": str(row.id),
        "type": channel_type,
    }
