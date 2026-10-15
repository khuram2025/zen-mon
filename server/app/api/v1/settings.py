from app.services.notification_transport import NotificationHTTPClient
from app.services.sms_transport import send_sms_request
from app.services.alert_delivery_log import DeliveryRejected
from app.core.crypto import encrypt_config, decrypt_config
import ssl
import base64
from uuid import UUID
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, StrictBool, ValidationError, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_admin_user, require_permission
from app.models.user import User
from app.services import branding, device_status_explanation, device_status_machine
from app.services.audit_service import write_audit_log

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


class GatewayTestRequest(BaseModel):
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
    value = row[0] if row else None
    if value and key in ('auth.ldap', 'auth.radius', 'auth.oidc'):
        from app.core.crypto import decrypt_secret
        value = dict(value)
        field = {'auth.ldap': 'bind_password', 'auth.radius': 'secret', 'auth.oidc': 'client_secret'}[key]
        value[field] = decrypt_secret(value.get(field)) or ''
    return decrypt_config(value) if value and key in ("smtp", "sms") else value


async def _upsert_system_setting(db: AsyncSession, key: str, value: dict) -> None:
    if key in ("smtp", "sms"):
        value = encrypt_config(value)
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


async def _sync_default_notification_gateway(
    db: AsyncSession,
    gateway_type: str,
    name: str,
    config: dict,
) -> None:
    await db.execute(
        text(
            "INSERT INTO notification_gateways (name, type, config, is_default, enabled, created_at, updated_at) "
            "VALUES (:name, :type, CAST(:config AS jsonb), true, :enabled, NOW(), NOW()) "
            "ON CONFLICT (type) WHERE is_default "
            "DO UPDATE SET name = EXCLUDED.name, config = EXCLUDED.config, "
            "enabled = EXCLUDED.enabled, updated_at = NOW()"
        ),
        {
            "name": name,
            "type": gateway_type,
            "config": _json_dumps(encrypt_config(config)),
            "enabled": bool(config.get("enabled")),
        },
    )


# ---------------------------------------------------------------------------
# Company Settings
# ---------------------------------------------------------------------------

class CompanySettings(BaseModel):
    """Organisation profile, regional formats and report branding.

    Read back through :func:`app.services.branding.load_branding`, which every
    report renderer, the scheduler and report emails use."""
    company_name: str = Field("", max_length=120)
    company_address: str = Field("", max_length=500)
    company_email: str = Field("", max_length=254)
    company_phone: str = Field("", max_length=64)
    company_website: str = Field("", max_length=255)
    # Public URL of this appliance, used for "View full report" links in
    # report emails (APP_BASE_URL in the environment takes precedence).
    base_url: str = Field("", max_length=255)
    timezone: str = "UTC"
    date_format: str = "YYYY-MM-DD"
    time_format: str = "24h"
    week_start: str = "monday"
    brand_color: str = branding.DEFAULT_BRAND_COLOR
    report_footer: str = Field("", max_length=200)
    report_disclaimer: str = Field("", max_length=1000)
    confidentiality: str = ""
    paper_size: str = "A4"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        if not branding.valid_timezone(v):
            raise ValueError("Unknown timezone")
        return v

    @field_validator("date_format")
    @classmethod
    def _date_format(cls, v: str) -> str:
        if v not in branding.DATE_FORMATS:
            raise ValueError(f"Date format must be one of {', '.join(branding.DATE_FORMATS)}")
        return v

    @field_validator("time_format")
    @classmethod
    def _time_format(cls, v: str) -> str:
        if v not in branding.TIME_FORMATS:
            raise ValueError("Time format must be 24h or 12h")
        return v

    @field_validator("week_start")
    @classmethod
    def _week_start(cls, v: str) -> str:
        v = (v or "").lower()
        if v not in branding.WEEK_STARTS:
            raise ValueError("Week start must be monday, sunday or saturday")
        return v

    @field_validator("brand_color")
    @classmethod
    def _brand_color(cls, v: str) -> str:
        if not branding.valid_hex_color(v):
            raise ValueError("Brand colour must be a hex colour such as #4F6BF6")
        return v.upper()

    @field_validator("confidentiality")
    @classmethod
    def _confidentiality(cls, v: str) -> str:
        if v not in branding.CONFIDENTIALITY_LEVELS:
            raise ValueError("Unknown classification label")
        return v

    @field_validator("paper_size")
    @classmethod
    def _paper(cls, v: str) -> str:
        if v not in branding.PAPER_SIZES:
            raise ValueError("Paper size must be A4 or Letter")
        return v

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, v: str) -> str:
        v = (v or "").strip().rstrip("/")
        if v and not branding.valid_base_url(v):
            raise ValueError("Appliance URL must start with http:// or https://")
        return v

    @field_validator("company_name", "company_address", "company_email", "company_phone",
                     "company_website", "report_footer", "report_disclaimer")
    @classmethod
    def _strip(cls, v: str) -> str:
        return (v or "").strip()


def _company_from_stored(raw: Optional[dict]) -> dict:
    """Stored company values, sanitised so an out-of-range legacy value never
    breaks the settings page."""
    b = branding.branding_from_values(raw)
    return CompanySettings.model_construct(
        company_name=b.company_name, company_address=b.company_address,
        company_email=b.company_email, company_phone=b.company_phone,
        company_website=b.company_website, base_url=b.base_url, timezone=b.timezone,
        date_format=b.date_format, time_format=b.time_format,
        week_start=b.week_start, brand_color=b.brand_color,
        report_footer=b.report_footer, report_disclaimer=b.report_disclaimer,
        confidentiality=b.confidentiality, paper_size=b.paper_size,
    ).model_dump()


class GrafanaSettings(BaseModel):
    enabled: bool = False
    base_url: str = ""
    dashboard_template: str = ""  # e.g. "/d/abc123?var-host={hostname}&var-device_id={device_id}"


_MONITORING_KEYS = tuple(device_status_machine.DEFAULTS)
# null means "derived": 80 % of the degraded limit (round3).
_MONITORING_NULLABLE = ("degraded_rtt_clear_ms", "degraded_loss_clear_pct")
_MONITORING_NUMBERS = tuple(k for k in _MONITORING_KEYS if k != "flap_detection_enabled")


class MonitoringSettings(BaseModel):
    """Device status policy (system_settings key 'monitoring'): degraded limits,
    confirmation counts, recovery hysteresis and flap detection.

    The central poller reloads it on its device-sync cadence (~60 s) and the
    remote-sensor ingest reads it on every upload, both through the shared
    loader (device_status_machine.policy_from_settings and its Go twin), which
    clamps whatever is stored. Every key is optional: PUT merges into the
    stored document, so a client that sends some keys (older UIs send only the
    two limits) keeps the others. Ranges are per field here; the cross-field
    rules are checked on the merged document by the PUT.
    """
    # Loss floor is 1%: with a 3-packet check the real granularity is ~33%.
    degraded_rtt_ms: Optional[float] = Field(default=None, gt=0, le=10000, allow_inf_nan=False)
    degraded_loss_pct: Optional[float] = Field(default=None, ge=1, le=100, allow_inf_nan=False)
    degraded_rtt_clear_ms: Optional[float] = Field(default=None, gt=0, le=10000, allow_inf_nan=False)
    degraded_loss_clear_pct: Optional[float] = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    degraded_confirm_checks: Optional[int] = Field(default=None, ge=1, le=20)
    degraded_window_checks: Optional[int] = Field(default=None, ge=1, le=20)
    degraded_clear_checks: Optional[int] = Field(default=None, ge=1, le=20)
    down_confirm_checks: Optional[int] = Field(default=None, ge=1, le=10)
    sensor_down_confirm_checks: Optional[int] = Field(default=None, ge=1, le=10)
    up_confirm_checks: Optional[int] = Field(default=None, ge=1, le=10)
    flap_detection_enabled: Optional[StrictBool] = None
    flap_window_minutes: Optional[int] = Field(default=None, ge=5, le=1440)
    flap_start_changes: Optional[int] = Field(default=None, ge=3, le=64)
    flap_stop_changes: Optional[int] = Field(default=None, ge=0, le=63)
    flap_stable_minutes: Optional[int] = Field(default=None, ge=1, le=1440)

    @field_validator(*_MONITORING_NUMBERS, mode="before")
    @classmethod
    def _json_number(cls, value):
        # The loaders read only JSON numbers; "5" or true would silently mean
        # "default" there, so they are refused here.
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("must be a number")
        return value

    @model_validator(mode="after")
    def _only_recovery_limits_may_be_null(self):
        nulls = sorted(key for key in self.model_fields_set
                       if getattr(self, key) is None and key not in _MONITORING_NULLABLE)
        if nulls:
            raise ValueError(f"{', '.join(nulls)} cannot be null")
        return self


def _monitoring_conflicts(doc: dict) -> list[str]:
    """Cross-field rules of a (merged, per-field valid) monitoring document.
    A missing key takes its default, as the loaders do."""
    def value(key: str):
        return doc[key] if doc.get(key) is not None else device_status_machine.DEFAULTS[key]

    fmt = device_status_machine.fmt3
    problems = []
    rtt, loss = value("degraded_rtt_ms"), value("degraded_loss_pct")
    rtt_clear, loss_clear = doc.get("degraded_rtt_clear_ms"), doc.get("degraded_loss_clear_pct")
    if rtt_clear is not None and rtt_clear > rtt:
        problems.append(f"degraded_rtt_clear_ms ({fmt(rtt_clear)} ms) cannot be above "
                        f"degraded_rtt_ms ({fmt(rtt)} ms)")
    if loss_clear is not None and loss_clear > loss:
        problems.append(f"degraded_loss_clear_pct ({fmt(loss_clear)}%) cannot be above "
                        f"degraded_loss_pct ({fmt(loss)}%)")
    confirm, window = value("degraded_confirm_checks"), value("degraded_window_checks")
    if window < confirm:
        problems.append(f"degraded_window_checks ({window}) must be at least "
                        f"degraded_confirm_checks ({confirm})")
    start, stop = value("flap_start_changes"), value("flap_stop_changes")
    if stop >= start:
        problems.append(f"flap_stop_changes ({stop}) must be below flap_start_changes ({start})")
    flap_window, stable = value("flap_window_minutes"), value("flap_stable_minutes")
    if stable > flap_window:
        problems.append(f"flap_stable_minutes ({stable}) cannot exceed flap_window_minutes ({flap_window})")
    return problems


def _monitoring_value_valid(key: str, value) -> bool:
    try:
        MonitoringSettings.model_validate({key: value})
    except ValidationError:
        return False
    return True


def _monitoring_document(stored: dict) -> dict:
    """GET shape: every policy key (stored value when valid on its own, else the
    default) plus ``effective``, the loader's output with the derived recovery
    limits and the cross-field clamps applied (what the poller and the sensor
    ingest actually use)."""
    doc = {}
    for key, default in device_status_machine.DEFAULTS.items():
        value = stored.get(key, default)
        doc[key] = value if _monitoring_value_valid(key, value) else default
    doc["effective"] = device_status_machine.effective_policy_dict(
        device_status_machine.policy_from_settings(stored)
    )
    return doc


def _validation_message(exc: ValidationError, incoming: dict) -> str:
    parts = []
    for error in exc.errors():
        key = ".".join(str(part) for part in error.get("loc", ()) if part != "__root__")
        message = str(error.get("msg", "invalid")).removeprefix("Value error, ")
        if not key:
            parts.append(message)
        else:
            parts.append(f"{key}: {message}" if key in incoming else f"stored {key}: {message}")
    return "; ".join(parts)


async def _merge_system_setting(db: AsyncSession, key: str, patch: dict, user: User) -> None:
    """jsonb-merge ``patch`` into a stored object (keys it lacks are kept) and
    stamp updated_at/updated_by. No commit: the caller commits it together with
    its audit entry."""
    await db.execute(
        text("""INSERT INTO system_settings (key, value, updated_at, updated_by)
                VALUES (:key, CAST(:value AS jsonb), NOW(),
                        (SELECT id FROM users WHERE id = CAST(:user_id AS uuid)))
                ON CONFLICT (key) DO UPDATE
                   SET value = CASE WHEN jsonb_typeof(system_settings.value) = 'object'
                                    THEN system_settings.value ELSE '{}'::jsonb END
                               || EXCLUDED.value,
                       updated_at = EXCLUDED.updated_at,
                       updated_by = EXCLUDED.updated_by"""),
        {
            "key": key,
            "value": _json_dumps(patch),
            "user_id": str(user.id) if getattr(user, "id", None) else None,
        },
    )


@router.get("/monitoring")
async def get_monitoring_settings(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.view")),
):
    raw = await _get_system_setting(db, "monitoring")
    return _monitoring_document(dict(raw) if isinstance(raw, dict) else {})


@router.put("/monitoring")
async def update_monitoring_settings(
    data: MonitoringSettings,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.manage")),
):
    incoming = data.model_dump(exclude_unset=True)
    row = (await db.execute(
        text("SELECT value, updated_at FROM system_settings WHERE key = 'monitoring' FOR UPDATE")
    )).first()
    stored = dict(row[0]) if row and isinstance(row[0], dict) else {}
    merged = {**stored, **incoming}
    try:
        checked = MonitoringSettings.model_validate(
            {key: merged[key] for key in _MONITORING_KEYS if key in merged}
        )
    except ValidationError as exc:
        raise HTTPException(422, f"Invalid monitoring settings: {_validation_message(exc, incoming)}")
    problems = _monitoring_conflicts(checked.model_dump(exclude_unset=True))
    if problems:
        raise HTTPException(422, "Invalid monitoring settings: " + "; ".join(problems))

    # updated_at moves with every save (the UI sends the full document), so the status explanation
    # reads when the limits a sample is judged against last changed from a stamp: now when they
    # change, else the time it used until this save (carried over from a document written before
    # the stamp existed; null when there was no document). Server-set only: the model drops a
    # client's own value.
    explain = device_status_explanation
    if explain.thresholds_changed(stored, merged):
        changed_at = datetime.now(timezone.utc)
    else:
        changed_at = explain.thresholds_changed_at(stored, row[1] if row else None)
    patch = {**incoming, explain.THRESHOLDS_CHANGED_AT: changed_at.isoformat() if changed_at else None}
    await _merge_system_setting(db, "monitoring", patch, user)
    await write_audit_log(
        db,
        actor=user,
        action="settings.monitoring.update",
        resource_type="system_setting",
        resource_id="monitoring",
        metadata=incoming,
    )
    await db.commit()
    return {
        "message": "Monitoring settings updated. The poller applies them within about a minute; "
                   "remote sensor results use them from their next upload.",
        "effective": device_status_machine.effective_policy_dict(
            device_status_machine.policy_from_settings(merged)
        ),
    }


@router.get("/integrations/grafana")
async def get_grafana_settings(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.view", "maps.view")),
):
    """Operator-readable so the manual-map page can decide whether to show
    'Open in Grafana' affordances. Editing remains admin-only below."""
    raw = await _get_system_setting(db, "integrations.grafana")
    return GrafanaSettings(**(raw or {})).model_dump()


@router.put("/integrations/grafana")
async def update_grafana_settings(
    data: GrafanaSettings,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.manage")),
):
    await _upsert_system_setting(db, "integrations.grafana", data.model_dump())
    await write_audit_log(
        db,
        actor=user,
        action="settings.integrations.grafana.update",
        resource_type="system_setting",
        resource_id="integrations.grafana",
    )
    await db.commit()
    return {"message": "Grafana settings updated"}


@router.get("/company")
async def get_company_settings(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.view")),
):
    raw = await _get_system_setting(db, "company")
    out = _company_from_stored(raw)
    logos = {}
    for variant, key in branding.LOGO_KEYS.items():
        logo = branding.decode_logo(await _get_system_setting(db, key))
        logos[variant] = None if logo is None else {
            "content_type": logo.content_type, "filename": logo.filename,
            "size": len(logo.data), "updated_at": logo.updated_at,
        }
    out["logos"] = logos
    return out


@router.put("/company")
async def update_company_settings(
    data: CompanySettings,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.manage")),
):
    previous = await _get_system_setting(db, "company") or {}
    new = data.model_dump()
    # Keep keys this version does not know about (written by a newer release).
    merged = {**previous, **new} if isinstance(previous, dict) else new
    await _upsert_system_setting(db, "company", merged)
    changed = sorted(k for k in new if (previous or {}).get(k) != new[k]) if isinstance(previous, dict) else sorted(new)
    await write_audit_log(
        db,
        actor=user,
        action="settings.company.update",
        resource_type="system_setting",
        resource_id="company",
        metadata={"changed": changed},
    )
    await db.commit()
    return {"message": "Company settings updated"}


def _logo_variant(variant: str) -> str:
    if variant not in branding.LOGO_KEYS:
        raise HTTPException(status_code=400, detail="variant must be 'light' or 'dark'")
    return branding.LOGO_KEYS[variant]


@router.post("/company/logo")
async def upload_company_logo(
    file: UploadFile = File(...),
    variant: str = Query("light", description="light: for light backgrounds (reports, print); "
                                                "dark: for dark backgrounds (report cover)"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.manage")),
):
    key = _logo_variant(variant)
    content = await file.read(branding.LOGO_MAX_BYTES + 1)
    if len(content) > branding.LOGO_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Logo must be 2 MB or smaller")
    # Trust the bytes, not the client's declared type.
    content_type = branding.sniff_logo(content)
    if not content_type:
        raise HTTPException(
            status_code=400,
            detail="Upload a PNG, JPEG, WebP or SVG image. SVG files may not contain scripts, "
                   "event handlers or embedded objects.",
        )

    await _upsert_system_setting(db, key, {
        "data": base64.b64encode(content).decode(),
        "content_type": content_type,
        "filename": (file.filename or "")[:255],
        "updated_at": branding.utc_now_iso(),
    })
    await write_audit_log(
        db,
        actor=user,
        action="settings.company_logo.upload",
        resource_type="system_setting",
        resource_id=key,
        metadata={"filename": file.filename, "content_type": content_type,
                  "size": len(content), "variant": variant},
    )
    await db.commit()
    return {"message": "Logo uploaded", "variant": variant, "content_type": content_type}


@router.delete("/company/logo")
async def delete_company_logo(
    variant: str = Query("light"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.manage")),
):
    key = _logo_variant(variant)
    res = await db.execute(text("DELETE FROM system_settings WHERE key = :key"), {"key": key})
    if not res.rowcount:
        raise HTTPException(status_code=404, detail="No logo uploaded")
    await write_audit_log(
        db,
        actor=user,
        action="settings.company_logo.delete",
        resource_type="system_setting",
        resource_id=key,
        metadata={"variant": variant},
    )
    await db.commit()
    return {"message": "Logo removed", "variant": variant}


# An uploaded SVG is served from the application origin: never let it run
# script or load anything if someone opens the URL directly.
LOGO_RESPONSE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "public, max-age=300",
}


@router.get("/company/logo")
async def get_company_logo(
    variant: str = Query("light"),
    fallback: bool = Query(True, description="Serve the other variant when this one is not set"),
    db: AsyncSession = Depends(get_db),
):
    """Public: the login page and emailed reports show the organisation logo."""
    key = _logo_variant(variant)
    logo = branding.decode_logo(await _get_system_setting(db, key))
    if logo is None and fallback:
        other = "dark" if variant == "light" else "light"
        logo = branding.decode_logo(await _get_system_setting(db, branding.LOGO_KEYS[other]))
    if logo is None:
        raise HTTPException(status_code=404, detail="No logo uploaded")
    return Response(content=logo.data, media_type=logo.content_type, headers=LOGO_RESPONSE_HEADERS)


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
        "config": decrypt_config(row.config),
        "enabled": row.enabled,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if hasattr(row, 'gateway_id'):
        d["gateway_id"] = str(row.gateway_id) if row.gateway_id else None
        if row.gateway_id:
            d["config"]["gateway_id"] = str(row.gateway_id)
    if hasattr(row, 'gateway_name'):
        d["gateway_name"] = row.gateway_name if row.gateway_name else None
    return d


def _row_to_gateway(row) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "type": row.type,
        "config": decrypt_config(row.config),
        "is_default": row.is_default,
        "enabled": row.enabled,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Shared gateway-test helpers (used by both the legacy and per-gateway routes)
# ---------------------------------------------------------------------------

def _normalize_smtp_config(raw: dict) -> dict:
    """Map legacy use_tls/use_ssl booleans onto the single `encryption` field so
    gateways saved by older builds still test/send correctly."""
    cfg = dict(raw or {})
    if not cfg.get("encryption"):
        if cfg.get("use_ssl"):
            cfg["encryption"] = "ssl"
        elif cfg.get("use_tls"):
            cfg["encryption"] = "tls"
    return cfg


def _do_smtp_test(raw_config: dict, recipient: str) -> dict:
    """Send a test email using the given SMTP config dict. Raises HTTPException on failure."""
    config = SmtpConfig(**_normalize_smtp_config(raw_config))
    recipients = [r.strip() for r in (recipient or "").split(",") if r.strip()]
    if not recipients:
        raise HTTPException(status_code=400, detail="A recipient email address is required")
    if not config.host:
        raise HTTPException(status_code=400, detail="SMTP host is required")
    if not config.from_email:
        raise HTTPException(status_code=400, detail="SMTP 'From' address is required")

    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    try:
        from app.services.email_render import (
            build_notification_email_html, build_notification_email_text,
        )
        test_ctx = {
            "status": "TEST",
            "title": "SMTP configuration verified",
            "message": "This is a test email from ZenPlus. If you are reading it, "
                       "this SMTP gateway can deliver mail to your inbox and is "
                       "ready to carry alerts, reports, and notifications.",
            "details": [
                ("Gateway", config.host + (f":{config.port}" if config.port else "")),
                ("Encryption", (config.encryption or "none").upper()),
                ("From", f"{config.from_name or 'ZenPlus'} <{config.from_email}>"),
            ],
        }
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{config.from_name or 'ZenPlus'} <{config.from_email}>"
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = "ZenPlus SMTP Test — configuration verified"
        msg.attach(MIMEText(build_notification_email_text(test_ctx), "plain"))
        msg.attach(MIMEText(build_notification_email_html(test_ctx), "html"))

        if config.encryption == "ssl":
            server = smtplib.SMTP_SSL(config.host, config.port or 465, timeout=15, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(config.host, config.port or 587, timeout=15)
            server.starttls(context=ssl.create_default_context())
        try:
            if config.username:
                server.login(config.username, config.password)
            server.sendmail(config.from_email, recipients, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass

        return {"message": f"Test email sent to {', '.join(recipients)}", "recipient": recipient}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"SMTP test failed — {type(e).__name__}: {e}")


async def _do_sms_test(raw_config: dict, recipient: str) -> dict:
    """Send a test SMS using the given SMS config dict. Raises HTTPException on failure."""
    config = SmsConfig(**(raw_config or {}))
    try:
        response = await send_sms_request(config.model_dump(), {
            'recipients': recipient,
            'message': 'ZenPlus Test Alert: This is a test SMS from your monitoring system.',
            'sender': config.sender_name or 'ZenPlus',
            'hostname': 'test-device', 'ip_address': '0.0.0.0', 'status': 'TEST',
        }, client_factory=NotificationHTTPClient)
    except DeliveryRejected as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except ValueError as exc:
        # Only locally generated validation messages are exposed.
        detail = str(exc) if str(exc).startswith(('SMS gateway', 'A recipient', 'This SMS provider', 'Notification gateways')) else 'SMS gateway configuration is invalid'
        raise HTTPException(status_code=400, detail=detail) from None
    except Exception:
        raise HTTPException(status_code=502, detail='SMS gateway connection failed') from None
    return {'message': 'SMS test accepted by the provider', 'status_code': response.status_code}


async def _clear_default_gateways(db: AsyncSession, gateway_type: str, keep_id=None) -> None:
    """Ensure at most one default gateway per type (the partial unique index
    idx_notification_gateways_default_per_type enforces this at the DB level)."""
    if keep_id is not None:
        await db.execute(
            text("UPDATE notification_gateways SET is_default = false, updated_at = NOW() "
                 "WHERE type = :type AND is_default = true AND id <> :keep_id"),
            {"type": gateway_type, "keep_id": keep_id},
        )
    else:
        await db.execute(
            text("UPDATE notification_gateways SET is_default = false, updated_at = NOW() "
                 "WHERE type = :type AND is_default = true"),
            {"type": gateway_type},
        )


# ---------------------------------------------------------------------------
# Legacy gateway endpoints — MUST be before parameterized {gateway_id} routes
# ---------------------------------------------------------------------------

@router.get("/gateways", response_model=GatewaysResponse)
async def get_gateways(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.manage")),
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
    user: User = Depends(require_permission("settings.manage")),
):
    await _upsert_system_setting(db, "smtp", data.model_dump())
    await _sync_default_notification_gateway(db, "smtp", "Default SMTP", data.model_dump())
    await write_audit_log(
        db,
        actor=user,
        action="settings.gateway.smtp.update",
        resource_type="system_setting",
        resource_id="smtp",
        metadata={"enabled": data.enabled, "host": data.host, "port": data.port},
    )
    await db.commit()
    return {"message": "SMTP settings updated"}


@router.put("/gateways/sms")
async def update_sms_legacy(
    data: SmsConfig,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.manage")),
):
    await _upsert_system_setting(db, "sms", data.model_dump())
    await _sync_default_notification_gateway(db, "sms", "Default SMS", data.model_dump())
    await write_audit_log(
        db,
        actor=user,
        action="settings.gateway.sms.update",
        resource_type="system_setting",
        resource_id="sms",
        metadata={"enabled": data.enabled, "provider": data.provider},
    )
    await db.commit()
    return {"message": "SMS settings updated"}


@router.post("/gateways/smtp/test")
async def test_smtp_legacy(
    data: SmtpTestRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.manage")),
):
    smtp_raw = await _get_system_setting(db, "smtp")
    if not smtp_raw:
        raise HTTPException(status_code=400, detail="SMTP not configured")
    return _do_smtp_test(smtp_raw, data.recipient)


@router.post("/gateways/sms/test")
async def test_sms_legacy(
    data: SmsTestRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.manage")),
):
    sms_raw = await _get_system_setting(db, "sms")
    if not sms_raw:
        raise HTTPException(status_code=400, detail="SMS not configured")
    return await _do_sms_test(sms_raw, data.recipient)


# ---------------------------------------------------------------------------
# Gateway endpoints (multi-gateway CRUD)
# ---------------------------------------------------------------------------

@router.get("/gateways/list")
async def list_gateways(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.manage")),
):
    result = await db.execute(
        text("SELECT id, name, type, config, is_default, enabled, created_at, updated_at FROM notification_gateways ORDER BY type, name")
    )
    return {"data": [_row_to_gateway(r) for r in result.fetchall()]}


@router.post("/gateways", status_code=201)
async def create_gateway(
    data: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.manage")),
):
    now = datetime.now(timezone.utc)
    gw_type = data.get("type", "smtp")
    if gw_type not in ("smtp", "sms"):
        raise HTTPException(status_code=400, detail="Gateway type must be 'smtp' or 'sms'")
    if not (data.get("name") or "").strip():
        raise HTTPException(status_code=400, detail="Gateway name is required")
    is_default = bool(data.get("is_default", False))
    # The DB has a partial unique index allowing one default per type; demote any
    # existing default of this type first so setting a new default never 500s.
    if is_default:
        await _clear_default_gateways(db, gw_type)
    result = await db.execute(
        text(
            "INSERT INTO notification_gateways (name, type, config, is_default, enabled, created_at, updated_at) "
            "VALUES (:name, :type, CAST(:config AS jsonb), :is_default, :enabled, :created_at, :updated_at) "
            "RETURNING id, name, type, config, is_default, enabled, created_at, updated_at"
        ),
        {
            "name": data.get("name", ""),
            "type": gw_type,
            "config": _json_dumps(encrypt_config(data.get("config", {}))),
            "is_default": is_default,
            "enabled": data.get("enabled", True),
            "created_at": now,
            "updated_at": now,
        },
    )
    row = result.first()
    await write_audit_log(
        db,
        actor=user,
        action="notification_gateway.create",
        resource_type="notification_gateway",
        resource_id=str(row.id) if row else None,
        metadata={"name": data.get("name", ""), "type": data.get("type", "smtp")},
    )
    await db.commit()
    return _row_to_gateway(row)


@router.put("/gateways/{gateway_id}")
async def update_gateway(
    gateway_id: UUID,
    data: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.manage")),
):
    fields = {k: v for k, v in data.items() if k in ("name", "type", "config", "is_default", "enabled")}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Promoting this gateway to default: demote the current default of the same
    # type first (partial unique index allows only one default per type).
    if fields.get("is_default"):
        type_row = (await db.execute(
            text("SELECT type FROM notification_gateways WHERE id = :id"), {"id": gateway_id}
        )).first()
        if not type_row:
            raise HTTPException(status_code=404, detail="Gateway not found")
        gw_type = fields.get("type") or type_row.type
        await _clear_default_gateways(db, gw_type, keep_id=gateway_id)

    set_parts = ["updated_at = :updated_at"]
    params: dict = {"id": gateway_id, "updated_at": datetime.now(timezone.utc)}

    for key, value in fields.items():
        if key == "config":
            set_parts.append("config = CAST(:config AS jsonb)")
            params["config"] = _json_dumps(encrypt_config(value))
        else:
            set_parts.append(f"{key} = :{key}")
            params[key] = value

    # Mirror `enabled` into the config blob. The column is authoritative, but
    # older rows and any external reader still look at config.enabled, and the
    # two silently disagreeing is what made an enabled gateway report itself
    # disabled. Only touch the key when config is not being rewritten wholesale.
    if "enabled" in fields and "config" not in fields:
        # The cast is required: asyncpg sends bare parameters as `unknown`, and
        # to_jsonb() is polymorphic, so Postgres cannot resolve the overload.
        set_parts.append(
            "config = jsonb_set(COALESCE(config, '{}'::jsonb), '{enabled}', "
            "to_jsonb(CAST(:enabled_flag AS boolean)))")
        params["enabled_flag"] = bool(fields["enabled"])
    elif "enabled" in fields and "config" in fields:
        merged = dict(fields.get("config") or {})
        merged["enabled"] = bool(fields["enabled"])
        params["config"] = _json_dumps(encrypt_config(merged))

    result = await db.execute(
        text(f"UPDATE notification_gateways SET {', '.join(set_parts)} WHERE id = :id "
             "RETURNING id, name, type, config, is_default, enabled, created_at, updated_at"),
        params,
    )
    row = result.first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Gateway not found")
    await write_audit_log(
        db,
        actor=user,
        action="notification_gateway.update",
        resource_type="notification_gateway",
        resource_id=str(gateway_id),
        metadata={"fields": sorted(fields.keys())},
    )
    await db.commit()
    return _row_to_gateway(row)


@router.delete("/gateways/{gateway_id}", status_code=204)
async def delete_gateway(
    gateway_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.manage")),
):
    result = await db.execute(
        text("DELETE FROM notification_gateways WHERE id = :id"), {"id": gateway_id}
    )
    if result.rowcount == 0:
        await db.commit()
        raise HTTPException(status_code=404, detail="Gateway not found")
    await write_audit_log(
        db,
        actor=user,
        action="notification_gateway.delete",
        resource_type="notification_gateway",
        resource_id=str(gateway_id),
    )
    await db.commit()


@router.post("/gateways/{gateway_id}/test")
async def test_gateway(
    gateway_id: UUID,
    data: GatewayTestRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("settings.manage")),
):
    """Send a test message through a specific saved gateway (not just the default)."""
    row = (await db.execute(
        text("SELECT type, config FROM notification_gateways WHERE id = :id"),
        {"id": gateway_id},
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Gateway not found")

    config = decrypt_config(row.config)
    if row.type == "smtp":
        return _do_smtp_test(config, data.recipient)
    if row.type == "sms":
        return await _do_sms_test(config, data.recipient)
    raise HTTPException(status_code=400, detail=f"Unsupported gateway type: {row.type}")


# ---------------------------------------------------------------------------
# Notification channel endpoints
# ---------------------------------------------------------------------------

@router.get("/channels")
async def list_channels(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("alerts.manage", "settings.manage")),
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
    user: User = Depends(require_permission("alerts.manage", "settings.manage")),
):
    now = datetime.now(timezone.utc)
    gateway_id = (data.config.get("gateway_id") or None) if data.config else None
    result = await db.execute(
        text(
            "INSERT INTO notification_channels (name, type, config, enabled, gateway_id, created_at, updated_at) "
            "VALUES (:name, :type, CAST(:config AS jsonb), :enabled, :gateway_id, :created_at, :updated_at) "
            "RETURNING id, name, type, config, enabled, gateway_id, created_at, updated_at"
        ),
        {
            "name": data.name,
            "type": data.type,
            "config": _json_dumps(encrypt_config(data.config)),
            "enabled": data.enabled,
            "gateway_id": gateway_id,
            "created_at": now,
            "updated_at": now,
        },
    )
    row = result.first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=500, detail="Failed to create channel")
    await write_audit_log(
        db,
        actor=user,
        action="notification_channel.create",
        resource_type="notification_channel",
        resource_id=str(row.id),
        metadata={"name": data.name, "type": data.type, "enabled": data.enabled},
    )
    await db.commit()
    return _row_to_channel(row)


@router.put("/channels/{channel_id}")
async def update_channel(
    channel_id: UUID,
    data: ChannelUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("alerts.manage", "settings.manage")),
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
            params["config"] = _json_dumps(encrypt_config(value))
            set_parts.append("gateway_id = :gateway_id")
            params["gateway_id"] = (value.get("gateway_id") or None) if isinstance(value, dict) else None
        else:
            set_parts.append(f"{key} = :{key}")
            params[key] = value

    set_parts.append("updated_at = :updated_at")
    set_clause = ", ".join(set_parts)

    result = await db.execute(
        text(
            f"UPDATE notification_channels SET {set_clause} "
            "WHERE id = :id "
            "RETURNING id, name, type, config, enabled, gateway_id, created_at, updated_at"
        ),
        params,
    )
    row = result.first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Channel not found")
    await write_audit_log(
        db,
        actor=user,
        action="notification_channel.update",
        resource_type="notification_channel",
        resource_id=str(channel_id),
        metadata={"fields": sorted(fields.keys())},
    )
    await db.commit()
    return _row_to_channel(row)


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("alerts.manage", "settings.manage")),
):
    result = await db.execute(
        text("DELETE FROM notification_channels WHERE id = :id RETURNING id"),
        {"id": channel_id},
    )
    row = result.first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Channel not found")
    await write_audit_log(
        db,
        actor=user,
        action="notification_channel.delete",
        resource_type="notification_channel",
        resource_id=str(channel_id),
    )
    await db.commit()


@router.post("/channels/{channel_id}/test")
async def test_channel(
    channel_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("alerts.manage", "settings.manage")),
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

    config = decrypt_config(row.config)
    channel_type = row.type
    test_message = "ZenPlus Test: This is a test notification from your monitoring system."

    # ─── SMS Channel ───
    if channel_type == "sms":
        phone_numbers = config.get("phone_numbers", "")
        if not phone_numbers:
            raise HTTPException(status_code=400, detail="SMS channel has no phone numbers configured")

        # Find the gateway (linked or default)
        # As with SMTP below: `enabled` is the column the Gateways page writes.
        gw_id = row.gateway_id or config.get("gateway_id")
        if gw_id:
            gw_result = await db.execute(
                text("SELECT config, enabled FROM notification_gateways WHERE id = :id AND type = :type"),
                {"id": gw_id, "type": "sms" if channel_type == "sms" else "smtp"})
        else:
            gw_result = await db.execute(text(
                "SELECT config, enabled FROM notification_gateways "
                "WHERE type = 'sms' AND is_default = true LIMIT 1"))
        gw_row = gw_result.first()

        if not gw_row:
            if gw_id:
                raise HTTPException(status_code=400, detail="Selected SMS gateway is unavailable")
            # Fallback to system_settings
            gw_config = await _get_system_setting(db, "sms")
            if not gw_config:
                raise HTTPException(status_code=400, detail="No SMS gateway configured")
            gw_enabled = bool(gw_config.get("enabled", True))
        else:
            gw_config = decrypt_config(gw_row.config)
            gw_enabled = bool(gw_row.enabled)

        sms_cfg = SmsConfig(**gw_config)
        if not gw_enabled:
            raise HTTPException(status_code=400, detail="SMS gateway is disabled")

        return await _do_sms_test(sms_cfg.model_dump(), phone_numbers)

    # ─── Email Channel ───
    elif channel_type == "email":
        recipients = config.get("recipients", "")
        if not recipients:
            raise HTTPException(status_code=400, detail="Email channel has no recipients configured")

        # Find SMTP gateway. Select `enabled` too: it is a column on
        # notification_gateways and the Gateways page toggles it there, while
        # config.enabled is only whatever was in the blob when it was last
        # saved. Reading the blob made an enabled gateway test as "disabled".
        gw_id = row.gateway_id or config.get("gateway_id")
        if gw_id:
            gw_result = await db.execute(
                text("SELECT config, enabled FROM notification_gateways WHERE id = :id AND type = :type"),
                {"id": gw_id, "type": "sms" if channel_type == "sms" else "smtp"})
        else:
            gw_result = await db.execute(text(
                "SELECT config, enabled FROM notification_gateways "
                "WHERE type = 'smtp' AND is_default = true LIMIT 1"))
        gw_row = gw_result.first()

        if not gw_row:
            gw_config = await _get_system_setting(db, "smtp")
            if not gw_config:
                raise HTTPException(status_code=400, detail="No SMTP gateway configured")
            # Legacy system_settings has no column; absent means enabled, which
            # is what the sending path assumes too.
            gw_enabled = bool(gw_config.get("enabled", True))
        else:
            gw_config = decrypt_config(gw_row.config)
            gw_enabled = bool(gw_row.enabled)

        smtp_cfg = SmtpConfig(**_normalize_smtp_config(gw_config))
        if not gw_enabled:
            raise HTTPException(status_code=400, detail="SMTP gateway is disabled")
        if not smtp_cfg.host:
            raise HTTPException(status_code=400, detail="SMTP gateway host not configured")

        recipient_list = [r.strip() for r in recipients.split(",") if r.strip()]
        try:
            from app.services.email_render import (
                build_notification_email_html, build_notification_email_text,
            )
            test_ctx = {
                "status": "TEST",
                "title": "Notification channel verified",
                "message": "This is a test email from ZenPlus. If you are reading it, "
                           f"the '{row.name}' notification channel is configured "
                           "correctly and will deliver alerts to these recipients.",
                "details": [("Channel", row.name), ("Recipients", recipients)],
            }
            msg = MIMEMultipart("alternative")
            msg["From"] = f"{smtp_cfg.from_name} <{smtp_cfg.from_email}>"
            msg["To"] = ", ".join(recipient_list)
            msg["Subject"] = f"ZenPlus Test Notification — {row.name}"
            msg.attach(MIMEText(build_notification_email_text(test_ctx), "plain"))
            msg.attach(MIMEText(build_notification_email_html(test_ctx), "html"))

            if smtp_cfg.encryption == "ssl":
                server = smtplib.SMTP_SSL(smtp_cfg.host, smtp_cfg.port, timeout=10, context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(smtp_cfg.host, smtp_cfg.port, timeout=10)
                server.starttls(context=ssl.create_default_context())
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
            async with NotificationHTTPClient(timeout=10.0) as client:
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
            async with NotificationHTTPClient(timeout=10.0) as client:
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
            async with NotificationHTTPClient(timeout=10.0) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": test_message},
                )
            return {"message": f"Telegram message sent. Status: {resp.status_code}"}
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Telegram failed: {str(e)}")

    else:
        raise HTTPException(status_code=400, detail=f"Unknown channel type: {channel_type}")
