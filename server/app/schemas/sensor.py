"""Pydantic schemas for the sensor API.

Two surfaces:
  * Admin / dashboard endpoints (/api/v1/sensors)  — JWT auth.
  * Sensor-facing endpoints     (/api/v1/sensor/*) — bearer-token auth.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Literal, List, Any
from uuid import UUID

from pydantic import BaseModel, Field


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
    sensor_cidr: Optional[int] = Field(default=None, ge=1, le=32)
    gateway: Optional[str] = None
    dns_servers: List[str] = Field(default_factory=list)
    proxy_url: Optional[str] = None
    enable_console_user: bool = False
    console_username: Optional[str] = Field(default=None, min_length=1, max_length=32)
    console_password: Optional[str] = Field(default=None, min_length=8, max_length=128)


class SensorUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    site_id: Optional[UUID] = None
    location: Optional[str] = None
    tags: Optional[List[str]] = None
    status: Optional[Literal["pending", "online", "degraded", "offline", "disabled"]] = None


class SensorResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    site_id: Optional[str]
    site_name: Optional[str] = None
    location: Optional[str]
    status: str
    version: Optional[str]
    last_seen_at: Optional[datetime]
    last_heartbeat_at: Optional[datetime]
    last_ip: Optional[str]
    queue_depth: int
    queue_dropped_count: int
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


class HeartbeatRequest(BaseModel):
    version: Optional[str] = None
    uptime_seconds: Optional[int] = None
    queue_depth: int = 0
    queue_dropped_count: int = 0
    hostname: Optional[str] = None
    os_info: Optional[str] = None


class HeartbeatResponse(BaseModel):
    ok: bool = True
    server_time: datetime
    config_etag: Optional[str] = None
    has_commands: bool = False


class ConfigDevice(BaseModel):
    id: str
    hostname: str
    ip_address: str
    ping_enabled: bool
    ping_interval: int = 60
    snmp_enabled: bool = False


class ConfigServiceCheck(BaseModel):
    id: str
    name: str
    check_type: str
    target_host: Optional[str] = None
    target_port: Optional[int] = None
    target_url: Optional[str] = None
    http_method: Optional[str] = None
    http_expected_statuses: Optional[str] = None
    http_content_match: Optional[str] = None
    http_follow_redirects: Optional[bool] = None
    tls_warn_days: Optional[int] = None
    tls_critical_days: Optional[int] = None
    check_interval: int = 60
    timeout: int = 10
    retry_count: int = 1
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
    rtt_ms: Optional[float] = None
    ip_address: Optional[str] = None


class ServiceResultItem(BaseModel):
    service_check_id: UUID
    timestamp: datetime
    check_type: str
    is_up: bool
    response_ms: Optional[float] = None
    status_code: Optional[int] = None
    error: Optional[str] = None


class SnmpResultItem(BaseModel):
    device_id: UUID
    timestamp: datetime
    oid: str
    value: Any


class ResultsBatch(BaseModel):
    """Generic batch wrapper used by all /results/* endpoints."""
    idempotency_key: Optional[str] = None
    items: List[Any] = Field(default_factory=list)


class EventItem(BaseModel):
    type: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    timestamp: datetime
    data: dict = Field(default_factory=dict)


class EventsBatch(BaseModel):
    items: List[EventItem] = Field(default_factory=list)
