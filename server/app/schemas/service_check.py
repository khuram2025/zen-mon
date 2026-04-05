from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field


class ServiceCheckCreate(BaseModel):
    device_id: Optional[UUID] = None
    name: str = Field(..., max_length=255)
    check_type: str = Field(..., pattern="^(http|tcp|tls)$")
    enabled: bool = True
    target_host: str = Field(..., max_length=255)
    target_port: Optional[int] = Field(default=None, ge=1, le=65535)
    target_url: Optional[str] = Field(default=None, max_length=2048)
    http_method: str = Field(default="GET", pattern="^(GET|POST|HEAD|PUT)$")
    http_headers: dict = Field(default_factory=dict)
    http_body: Optional[str] = None
    http_expected_status: int = Field(default=200, ge=100, le=599)
    http_content_match: Optional[str] = None
    http_follow_redirects: bool = True
    tls_warn_days: int = Field(default=30, ge=1, le=365)
    tls_critical_days: int = Field(default=7, ge=1, le=365)
    check_interval: int = Field(default=60, ge=10, le=3600)
    timeout: int = Field(default=10, ge=1, le=60)
    description: Optional[str] = None


class ServiceCheckUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    enabled: Optional[bool] = None
    target_host: Optional[str] = Field(default=None, max_length=255)
    target_port: Optional[int] = Field(default=None, ge=1, le=65535)
    target_url: Optional[str] = Field(default=None, max_length=2048)
    http_method: Optional[str] = Field(default=None, pattern="^(GET|POST|HEAD|PUT)$")
    http_headers: Optional[dict] = None
    http_body: Optional[str] = None
    http_expected_status: Optional[int] = Field(default=None, ge=100, le=599)
    http_content_match: Optional[str] = None
    http_follow_redirects: Optional[bool] = None
    tls_warn_days: Optional[int] = Field(default=None, ge=1, le=365)
    tls_critical_days: Optional[int] = Field(default=None, ge=1, le=365)
    check_interval: Optional[int] = Field(default=None, ge=10, le=3600)
    timeout: Optional[int] = Field(default=None, ge=1, le=60)
    description: Optional[str] = None


class ServiceCheckResponse(BaseModel):
    id: UUID
    device_id: Optional[UUID] = None
    device_hostname: Optional[str] = None
    name: str
    check_type: str
    enabled: bool
    target_host: str
    target_port: Optional[int] = None
    target_url: Optional[str] = None
    http_method: str
    http_expected_status: int
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
