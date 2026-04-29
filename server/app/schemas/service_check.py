import re
from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field, field_validator


_STATUS_PATTERN_RE = re.compile(r"^\s*([1-5][0-9xX]{2}|[1-5][0-9]{2}-[1-5][0-9]{2})\s*$")


def _validate_statuses(value: Optional[str]) -> Optional[str]:
    """Validate a comma-separated list of status patterns.

    Each item may be an exact code (200), a wildcard (2xx, 4xx) or
    a range (200-299). Returns the cleaned, comma-joined string.
    """
    if value is None:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        return None
    cleaned: list[str] = []
    for p in parts:
        if not _STATUS_PATTERN_RE.match(p):
            raise ValueError(
                f"Invalid status pattern: '{p}'. "
                "Use exact codes (200), wildcards (2xx) or ranges (200-299)."
            )
        cleaned.append(p.lower())
    return ",".join(cleaned)


class ServiceCheckCreate(BaseModel):
    device_id: Optional[UUID] = None
    group_id: Optional[UUID] = None
    parent_check_id: Optional[UUID] = None
    name: str = Field(..., max_length=255)
    check_type: str = Field(..., pattern="^(http|tcp|tls|icmp|dns)$")
    level: int = Field(default=1, ge=1, le=3)
    config: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    retry_count: int = Field(default=1, ge=1, le=10)
    retry_delay_s: int = Field(default=30, ge=1, le=600)
    enabled: bool = True
    target_host: str = Field(..., max_length=255)
    target_port: Optional[int] = Field(default=None, ge=1, le=65535)
    target_url: Optional[str] = Field(default=None, max_length=2048)
    http_method: str = Field(default="GET", pattern="^(GET|POST|HEAD|PUT)$")
    http_headers: dict = Field(default_factory=dict)
    http_body: Optional[str] = None
    http_expected_status: int = Field(default=200, ge=100, le=599)
    http_expected_statuses: Optional[str] = Field(default=None, max_length=255)
    http_content_match: Optional[str] = None
    http_follow_redirects: bool = True
    tls_warn_days: int = Field(default=30, ge=1, le=365)
    tls_critical_days: int = Field(default=7, ge=1, le=365)
    check_interval: int = Field(default=60, ge=10, le=3600)
    timeout: int = Field(default=10, ge=1, le=60)
    description: Optional[str] = None

    @field_validator("http_expected_statuses")
    @classmethod
    def _check_statuses(cls, v):
        return _validate_statuses(v)


class ServiceCheckUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    group_id: Optional[UUID] = None
    parent_check_id: Optional[UUID] = None
    level: Optional[int] = Field(default=None, ge=1, le=3)
    config: Optional[dict] = None
    tags: Optional[list[str]] = None
    retry_count: Optional[int] = Field(default=None, ge=1, le=10)
    retry_delay_s: Optional[int] = Field(default=None, ge=1, le=600)
    enabled: Optional[bool] = None
    target_host: Optional[str] = Field(default=None, max_length=255)
    target_port: Optional[int] = Field(default=None, ge=1, le=65535)
    target_url: Optional[str] = Field(default=None, max_length=2048)
    http_method: Optional[str] = Field(default=None, pattern="^(GET|POST|HEAD|PUT)$")
    http_headers: Optional[dict] = None
    http_body: Optional[str] = None
    http_expected_status: Optional[int] = Field(default=None, ge=100, le=599)
    http_expected_statuses: Optional[str] = Field(default=None, max_length=255)
    http_content_match: Optional[str] = None
    http_follow_redirects: Optional[bool] = None
    tls_warn_days: Optional[int] = Field(default=None, ge=1, le=365)
    tls_critical_days: Optional[int] = Field(default=None, ge=1, le=365)
    check_interval: Optional[int] = Field(default=None, ge=10, le=3600)
    timeout: Optional[int] = Field(default=None, ge=1, le=60)
    description: Optional[str] = None

    @field_validator("http_expected_statuses")
    @classmethod
    def _check_statuses(cls, v):
        return _validate_statuses(v)


class ServiceCheckResponse(BaseModel):
    id: UUID
    device_id: Optional[UUID] = None
    device_hostname: Optional[str] = None
    group_id: Optional[UUID] = None
    group_name: Optional[str] = None
    parent_check_id: Optional[UUID] = None
    parent_check_name: Optional[str] = None
    name: str
    check_type: str
    level: int = 1
    config: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    retry_count: int = 1
    retry_delay_s: int = 30
    in_maintenance: bool = False
    enabled: bool
    target_host: str
    target_port: Optional[int] = None
    target_url: Optional[str] = None
    http_method: str
    http_expected_status: int
    http_expected_statuses: Optional[str] = None
    http_content_match: Optional[str] = None
    http_follow_redirects: bool
    tls_warn_days: int
    tls_critical_days: int
    check_interval: int
    timeout: int
    status: str
    last_check_at: Optional[datetime] = None
    last_response_ms: Optional[float] = None
    last_error: Optional[str] = None
    tls_expiry_date: Optional[datetime] = None
    tls_days_remaining: Optional[int] = None
    tls_issuer: Optional[str] = None
    tls_subject: Optional[str] = None
    description: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ServiceCheckSummary(BaseModel):
    total: int
    up: int
    down: int
    warning: int
    degraded: int
    unknown: int


class ServiceMetricPoint(BaseModel):
    timestamp: datetime
    response_ms: Optional[float] = None
    is_up: Optional[bool] = None
    status_code: Optional[int] = None
    tls_days_remaining: Optional[int] = None
    error_message: Optional[str] = None


class ServiceMetricResponse(BaseModel):
    service_check_id: UUID
    granularity: str
    from_time: datetime
    to_time: datetime
    points: list[ServiceMetricPoint]


class ServiceCheckGroupCreate(BaseModel):
    name: str = Field(..., max_length=120)
    description: Optional[str] = None
    color: Optional[str] = Field(default=None, max_length=20)


class ServiceCheckGroupUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = None
    color: Optional[str] = Field(default=None, max_length=20)


class ServiceCheckGroupResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    color: Optional[str] = None
    check_count: int = 0
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ServiceMaintenanceCreate(BaseModel):
    scope_type: str = Field(..., pattern="^(check|group|tag|all)$")
    scope_check_id: Optional[UUID] = None
    scope_group_id: Optional[UUID] = None
    scope_tag: Optional[str] = Field(default=None, max_length=120)
    starts_at: datetime
    ends_at: datetime
    reason: Optional[str] = None


class ServiceCheckTemplateCreate(BaseModel):
    name: str = Field(..., max_length=120)
    description: Optional[str] = None
    check_type: str = Field(..., pattern="^(http|tcp|tls|icmp|dns)$")
    level: int = Field(default=1, ge=1, le=3)
    config: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    default_interval: int = Field(default=60, ge=10, le=3600)
    default_timeout: int = Field(default=10, ge=1, le=60)
    default_retry_count: int = Field(default=1, ge=1, le=10)
    default_retry_delay_s: int = Field(default=30, ge=1, le=600)
    target_url_template: Optional[str] = Field(default=None, max_length=2048)
    target_port_default: Optional[int] = Field(default=None, ge=1, le=65535)
    http_method: Optional[str] = Field(default=None, pattern="^(GET|POST|HEAD|PUT)$")
    http_expected_status: Optional[int] = Field(default=None, ge=100, le=599)
    http_expected_statuses: Optional[str] = Field(default=None, max_length=255)
    http_content_match: Optional[str] = None
    http_follow_redirects: Optional[bool] = None
    tls_warn_days: Optional[int] = Field(default=None, ge=1, le=365)
    tls_critical_days: Optional[int] = Field(default=None, ge=1, le=365)

    @field_validator("http_expected_statuses")
    @classmethod
    def _check_statuses(cls, v):
        return _validate_statuses(v)


class ServiceCheckTemplateUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = None
    level: Optional[int] = Field(default=None, ge=1, le=3)
    config: Optional[dict] = None
    tags: Optional[list[str]] = None
    default_interval: Optional[int] = Field(default=None, ge=10, le=3600)
    default_timeout: Optional[int] = Field(default=None, ge=1, le=60)
    default_retry_count: Optional[int] = Field(default=None, ge=1, le=10)
    default_retry_delay_s: Optional[int] = Field(default=None, ge=1, le=600)
    target_url_template: Optional[str] = Field(default=None, max_length=2048)
    target_port_default: Optional[int] = Field(default=None, ge=1, le=65535)
    http_method: Optional[str] = Field(default=None, pattern="^(GET|POST|HEAD|PUT)$")
    http_expected_status: Optional[int] = Field(default=None, ge=100, le=599)
    http_expected_statuses: Optional[str] = Field(default=None, max_length=255)
    http_content_match: Optional[str] = None
    http_follow_redirects: Optional[bool] = None
    tls_warn_days: Optional[int] = Field(default=None, ge=1, le=365)
    tls_critical_days: Optional[int] = Field(default=None, ge=1, le=365)

    @field_validator("http_expected_statuses")
    @classmethod
    def _check_statuses(cls, v):
        return _validate_statuses(v)


class ServiceCheckTemplateResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    check_type: str
    level: int
    config: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    default_interval: int
    default_timeout: int
    default_retry_count: int
    default_retry_delay_s: int
    target_url_template: Optional[str] = None
    target_port_default: Optional[int] = None
    http_method: Optional[str] = None
    http_expected_status: Optional[int] = None
    http_expected_statuses: Optional[str] = None
    http_content_match: Optional[str] = None
    http_follow_redirects: Optional[bool] = None
    tls_warn_days: Optional[int] = None
    tls_critical_days: Optional[int] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ServiceCheckTemplateApply(BaseModel):
    device_ids: list[UUID] = Field(default_factory=list)
    group_id: Optional[UUID] = None
    name_prefix: Optional[str] = Field(default=None, max_length=200)
    enabled: bool = True


class ServiceCheckTemplateApplyResult(BaseModel):
    created_ids: list[UUID]
    skipped: list[dict]  # [{device_id, reason}]


class ServiceMaintenanceResponse(BaseModel):
    id: UUID
    scope_type: str
    scope_check_id: Optional[UUID] = None
    scope_group_id: Optional[UUID] = None
    scope_tag: Optional[str] = None
    scope_label: Optional[str] = None  # human label for UI ("All checks", group name, etc.)
    starts_at: datetime
    ends_at: datetime
    reason: Optional[str] = None
    active: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}
