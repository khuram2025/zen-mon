"""Pydantic schemas for the sensor API.

Two surfaces:
  * Admin / dashboard endpoints (/api/v1/sensors)  — JWT auth.
  * Sensor-facing endpoints     (/api/v1/sensor/*) — bearer-token auth.
"""

from __future__ import annotations

from datetime import datetime
import ipaddress
from typing import Optional, Literal, List, Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Sites ────────────────────────────────────────────────────────────

class SiteCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    region: Optional[str] = None
    timezone: Optional[str] = "UTC"
    address: Optional[str] = None
    notes: Optional[str] = None


class SiteUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    region: Optional[str] = None
    timezone: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None


class SiteResponse(BaseModel):
    id: str
    name: str
    region: Optional[str]
    timezone: str
    address: Optional[str]
    notes: Optional[str]
    sensor_count: int = 0
    created_at: datetime
    updated_at: datetime


# ── Sensors (admin view) ─────────────────────────────────────────────

class SensorCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    site_id: Optional[UUID] = None
    location: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    controller_url: Optional[str] = None
    network_mode: Literal["dhcp", "static"] = "dhcp"
    sensor_ip: Optional[str] = None
    sensor_cidr: Optional[int] = Field(default=None, ge=1, le=128)
    gateway: Optional[str] = None
    dns_servers: List[str] = Field(default_factory=list)
    proxy_url: Optional[str] = None
    enable_console_user: bool = False
    console_username: Optional[str] = Field(default=None, min_length=1, max_length=32)
    console_password: Optional[str] = Field(default=None, min_length=8, max_length=128)

    @field_validator("name")
    @classmethod
    def validate_sensor_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(char in value for char in ("\r", "\n", "\x00")):
            raise ValueError("must be a non-empty single-line name")
        return value

    @field_validator("controller_url", "proxy_url")
    @classmethod
    def validate_http_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("must be an HTTP(S) URL without credentials, query, or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("must be a controller/proxy origin without a path")
        return value

    @model_validator(mode="after")
    def validate_bootstrap_settings(self):
        if self.controller_url and urlsplit(self.controller_url).scheme != "https":
            raise ValueError("controller_url must use HTTPS")
        if self.enable_console_user:
            if not self.console_username or not self.console_password:
                raise ValueError("console_username and console_password are required when console access is enabled")
        elif self.console_username or self.console_password:
            raise ValueError("enable_console_user must be true when console credentials are supplied")

        if self.network_mode == "static":
            if not self.sensor_ip or self.sensor_cidr is None or not self.gateway:
                raise ValueError("static network mode requires sensor_ip, sensor_cidr, and gateway")
            sensor_ip = ipaddress.ip_address(self.sensor_ip)
            gateway = ipaddress.ip_address(self.gateway)
            if sensor_ip.version != gateway.version:
                raise ValueError("sensor_ip and gateway must use the same IP family")
            if self.sensor_cidr > sensor_ip.max_prefixlen:
                raise ValueError(f"sensor_cidr must be at most {sensor_ip.max_prefixlen} for IPv{sensor_ip.version}")
        for value in self.dns_servers:
            ipaddress.ip_address(value)
        return self


class SensorUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    site_id: Optional[UUID] = None
    location: Optional[str] = None
    tags: Optional[List[str]] = None

    @field_validator("name")
    @classmethod
    def validate_sensor_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value or any(char in value for char in ("\r", "\n", "\x00")):
            raise ValueError("must be a non-empty single-line name")
        return value


class SensorResponse(BaseModel):
    authorization_pending: bool = False
    id: str
    name: str
    description: Optional[str]
    site_id: Optional[str]
    site_name: Optional[str] = None
    location: Optional[str]
    status: str
    status_reason: Optional[str] = None
    version: Optional[str]
    last_seen_at: Optional[datetime]
    last_heartbeat_at: Optional[datetime]
    last_ip: Optional[str]
    queue_depth: int
    queue_dropped_count: int
    heartbeat_interval_s: int = 30
    degraded_after_s: int = 90
    offline_after_s: int = 180
    min_supported_version: Optional[str] = None
    hostname: Optional[str]
    os_info: Optional[str]
    uptime_seconds: Optional[int]
    api_key_prefix: Optional[str]
    enrollment_pending: bool = False
    enrollment_expires_at: Optional[datetime] = None
    assignment_count: int = 0
    tags: List[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class SensorTokenResponse(BaseModel):
    """Returned ONCE when a sensor is created or its token is regenerated.
    The plaintext enrollment_token is never stored — only its hash is.
    """
    sensor_id: str
    enrollment_token: str
    expires_at: datetime
    server_url: str
    install_command: str
    manifest_url: Optional[str] = None
    ova_url: Optional[str] = None
    ovf_url: Optional[str] = None
    bootstrap_cloud_init: Optional[str] = None
    bootstrap_meta_data: Optional[str] = None
    bootstrap_network_config: Optional[str] = None
    bootstrap_iso_url: Optional[str] = None
    configured_ova_url: Optional[str] = None
    bootstrap_warning: Optional[str] = None


class SensorDownloadsResponse(BaseModel):
    sensor_id: str
    sensor_name: str
    manifest_url: Optional[str] = None
    ova_url: Optional[str] = None
    ovf_url: Optional[str] = None
    configured_ova_url: Optional[str] = None
    bootstrap_iso_url: Optional[str] = None
    configured_ova_size_bytes: Optional[int] = None
    bootstrap_iso_size_bytes: Optional[int] = None
    artifact_token: Optional[str] = None
    updated_at: Optional[datetime] = None
    note: Optional[str] = None


class SensorRotateKeyResponse(BaseModel):
    sensor_id: str
    api_key: str
    api_key_prefix: str
    rotated_at: datetime


class SensorEventResponse(BaseModel):
    id: str
    sensor_id: str
    ts: datetime
    kind: str
    detail: dict = Field(default_factory=dict)


class SensorCommandCreate(BaseModel):
    verb: Literal["update", "flush_buffer", "reload_config", "set_log_level"]
    payload: dict[str, Any] = Field(default_factory=dict)
    expires_in_seconds: int = Field(default=86400, ge=60, le=604800)

    @model_validator(mode="after")
    def validate_command_payload(self):
        allowed: dict[str, set[str]] = {
            "update": {"version"},
            "flush_buffer": set(),
            "reload_config": set(),
            "set_log_level": {"level"},
        }
        extra = set(self.payload) - allowed[self.verb]
        if extra:
            raise ValueError(f"unsupported {self.verb} payload fields: {', '.join(sorted(extra))}")
        if self.verb == "set_log_level":
            if self.payload.get("level") not in {"debug", "info", "warn", "error"}:
                raise ValueError("set_log_level requires level debug, info, warn, or error")
        return self


class SensorCommandResponse(BaseModel):
    id: str
    sensor_id: str
    verb: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str
    delivery_count: int
    last_delivered_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    expires_at: datetime
    result: Optional[str] = None
    created_at: datetime


class SensorVantageResponse(BaseModel):
    service_check_id: str
    service_check_name: str
    check_type: str
    state: str
    last_change_at: datetime
    last_result_at: datetime
    last_latency_ms: Optional[float] = None
    last_error: Optional[str] = None
    tls_days_remaining: Optional[int] = None


# ── Assignments ──────────────────────────────────────────────────────

class AssignmentCreate(BaseModel):
    target_type: Literal["device", "service_check", "group"]
    target_id: UUID
    priority: int = 100


class AssignmentBulk(BaseModel):
    """Replace the assignment list for a sensor in one shot."""
    items: List[AssignmentCreate] = Field(default_factory=list)


class AssignmentResponse(BaseModel):
    sensor_id: str
    target_type: str
    target_id: str
    target_name: Optional[str] = None
    priority: int
    created_at: datetime


# ── Sensor-facing API ────────────────────────────────────────────────

class EnrollRequest(BaseModel):
    enrollment_token: str
    hostname: Optional[str] = None
    os_info: Optional[str] = None
    version: Optional[str] = None


class EnrollResponse(BaseModel):
    sensor_id: str
    api_key: str
    heartbeat_interval_s: int = 30
    config_poll_interval_s: int = 60
    controller_ca_sha256: Optional[str] = None


class HeartbeatRequest(BaseModel):
    version: Optional[str] = None
    uptime_seconds: Optional[int] = Field(default=None, ge=0)
    queue_depth: int = Field(default=0, ge=0)
    queue_dropped_count: int = Field(default=0, ge=0)
    hostname: Optional[str] = None
    os_info: Optional[str] = None
    config_etag: Optional[str] = Field(default=None, max_length=128)


class HeartbeatCommand(BaseModel):
    id: str
    verb: Literal["update", "flush_buffer", "reload_config", "set_log_level"]
    payload: dict[str, Any] = Field(default_factory=dict)


class HeartbeatResponse(BaseModel):
    ok: bool = True
    server_time: datetime
    config_etag: Optional[str] = None
    has_commands: bool = False
    min_supported_version: Optional[str] = None
    commands: List[HeartbeatCommand] = Field(default_factory=list)


class ConfigSNMP(BaseModel):
    version: str = "2c"
    port: int = 161
    community: Optional[str] = None
    v3_username: Optional[str] = None
    v3_context: Optional[str] = None
    v3_auth_protocol: Optional[str] = None
    v3_auth_passphrase: Optional[str] = None
    v3_priv_protocol: Optional[str] = None
    v3_priv_passphrase: Optional[str] = None
    timeout_ms: int = 2000
    retries: int = 1
    interval: int = 60
    oids: List[str] = Field(default_factory=lambda: ["1.3.6.1.2.1.1.3.0", "1.3.6.1.2.1.2.1.0"])


class ConfigDevice(BaseModel):
    id: str
    hostname: str
    ip_address: str
    ping_enabled: bool
    ping_interval: int = 60
    snmp_enabled: bool = False
    snmp: Optional[ConfigSNMP] = None


class ConfigWorkflowStep(BaseModel):
    name: str = ""
    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: Optional[str] = None
    expected_statuses: str = "200"
    content_match: Optional[str] = None
    follow_redirects: bool = True


class ConfigServiceCheck(BaseModel):
    credential_id: Optional[str] = None
    credential_auth_type: str = ""
    credential_username: str = Field(default="", repr=False)
    credential_secret: str = Field(default="", repr=False)
    credential_error: str = ""
    workflow_operator: str = "all"
    workflow_steps: List[ConfigWorkflowStep] = Field(default_factory=list)
    id: str
    name: str
    check_type: str
    target_host: Optional[str] = None
    target_port: Optional[int] = None
    target_url: Optional[str] = None
    http_method: Optional[str] = None
    http_headers: dict[str, str] = Field(default_factory=dict)
    http_body: Optional[str] = Field(default=None, max_length=262144)
    http_expected_status: int = Field(default=200, ge=100, le=599)
    http_expected_statuses: Optional[str] = None
    http_content_match: Optional[str] = None
    http_follow_redirects: Optional[bool] = None
    http_ignore_tls_errors: bool = False
    http_allow_insecure_auth: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    tls_warn_days: Optional[int] = None
    tls_critical_days: Optional[int] = None
    check_interval: int = 60
    timeout: int = 10
    retry_count: int = 1
    retry_delay_s: int = Field(default=30, ge=1, le=600)
    enabled: bool = True


class ConfigResponse(BaseModel):
    etag: str
    sensor_id: str
    sensor_name: str
    devices: List[ConfigDevice] = Field(default_factory=list)
    service_checks: List[ConfigServiceCheck] = Field(default_factory=list)


class PingResultItem(BaseModel):
    device_id: UUID
    timestamp: datetime
    is_up: bool
    rtt_ms: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    packet_loss: Optional[float] = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    jitter_ms: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    min_rtt_ms: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    max_rtt_ms: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    packets_sent: Optional[int] = Field(default=None, ge=0, le=1000)
    packets_received: Optional[int] = Field(default=None, ge=0, le=1000)
    ip_address: Optional[str] = None

    @field_validator("ip_address")
    @classmethod
    def validate_ip_address(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        return str(ipaddress.ip_address(value.strip()))

    @model_validator(mode="after")
    def validate_packet_counts(self):
        if (
            self.packets_sent is not None
            and self.packets_received is not None
            and self.packets_received > self.packets_sent
        ):
            raise ValueError("packets_received cannot exceed packets_sent")
        return self


class ServiceResultItem(BaseModel):
    service_check_id: UUID
    timestamp: datetime
    check_type: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_-]+$")
    is_up: bool
    response_ms: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    status_code: Optional[int] = Field(default=None, ge=100, le=599)
    tls_days_remaining: Optional[int] = Field(default=None, ge=-36500, le=36500)
    tls_valid: Optional[bool] = None
    tls_expiry_date: Optional[datetime] = None
    tls_issuer: Optional[str] = Field(default=None, max_length=512)
    tls_subject: Optional[str] = Field(default=None, max_length=512)
    content_matched: Optional[bool] = None
    error: Optional[str] = Field(default=None, max_length=2048)

    @field_validator("status_code", mode="before")
    @classmethod
    def normalize_non_http_status(cls, value: Any) -> Any:
        return None if value in (None, "", 0) else value


class SnmpResultItem(BaseModel):
    device_id: UUID
    timestamp: datetime
    oid: str = Field(min_length=1, max_length=255, pattern=r"^\.?[0-9]+(?:\.[0-9]+)*$")
    value: float = Field(allow_inf_nan=False)
    unit: str = Field(default="", max_length=32)


class ResultsBatch(BaseModel):
    """Generic batch wrapper used by all /results/* endpoints."""
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    items: List[Any] = Field(default_factory=list, max_length=1000)


class PingResultsBatch(ResultsBatch):
    items: List[PingResultItem] = Field(default_factory=list, max_length=1000)


class ServiceResultsBatch(ResultsBatch):
    items: List[ServiceResultItem] = Field(default_factory=list, max_length=1000)


class SnmpResultsBatch(ResultsBatch):
    items: List[SnmpResultItem] = Field(default_factory=list, max_length=1000)


class EventItem(BaseModel):
    type: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    timestamp: datetime
    data: dict = Field(default_factory=dict)


class EventsBatch(BaseModel):
    items: List[EventItem] = Field(default_factory=list, max_length=200)
