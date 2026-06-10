"""Pydantic schemas for Discovery v2 API."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


ScopeType = Literal["single_ip", "ip_range", "cidr", "multi", "csv"]
ImportMode = Literal["review", "auto_match", "ignore_match"]
ScheduleType = Literal["once_now", "once_future", "recurring", "cron"]
Frequency = Literal["hourly", "daily", "weekly", "monthly", "custom"]
TriggerType = Literal["manual", "scheduled", "api", "retry"]
RunStatus = Literal["queued", "running", "completed", "failed", "cancelled", "partial"]
ResultStatus = Literal["new", "existing", "changed", "unknown", "ignored", "failed", "imported"]
CredentialStatus = Literal["valid", "invalid", "not_tested", "partial", "permission_issue"]


# ─────────────── Profile ───────────────
class DiscoveryProfileBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    description: Optional[str] = None
    enabled: bool = True

    scope_type: ScopeType = "cidr"
    targets: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    collector_id: Optional[str] = None

    protocols: list[str] = Field(default_factory=lambda: ["icmp"])
    custom_ports: list[int] = Field(default_factory=list)
    snmp_credential_ids: list[UUID] = Field(default_factory=list)
    windows_credential_ids: list[UUID] = Field(default_factory=list)
    detect_lldp: bool = True
    detect_mac: bool = True
    detect_vendor: bool = True

    max_concurrency: int = Field(default=32, ge=1, le=512)
    scan_timeout_ms: int = Field(default=2000, ge=100, le=60000)
    retry_count: int = Field(default=1, ge=0, le=5)
    rate_limit_pps: int = Field(default=200, ge=1, le=10000)
    max_duration_sec: int = Field(default=1800, ge=10, le=86400)

    import_mode: ImportMode = "review"
    default_group_id: Optional[UUID] = None
    default_tags: list[str] = Field(default_factory=list)
    default_template_id: Optional[UUID] = None
    default_location: Optional[str] = None
    default_owner: Optional[str] = None
    enable_monitoring: bool = True
    keep_disabled: bool = False
    notify_recipients: list[str] = Field(default_factory=list)


class DiscoveryProfileCreate(DiscoveryProfileBase):
    schedule: Optional["DiscoveryScheduleCreate"] = None


class DiscoveryProfileUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    scope_type: Optional[ScopeType] = None
    targets: Optional[list[str]] = None
    exclusions: Optional[list[str]] = None
    collector_id: Optional[str] = None
    protocols: Optional[list[str]] = None
    custom_ports: Optional[list[int]] = None
    snmp_credential_ids: Optional[list[UUID]] = None
    windows_credential_ids: Optional[list[UUID]] = None
    detect_lldp: Optional[bool] = None
    detect_mac: Optional[bool] = None
    detect_vendor: Optional[bool] = None
    max_concurrency: Optional[int] = None
    scan_timeout_ms: Optional[int] = None
    retry_count: Optional[int] = None
    rate_limit_pps: Optional[int] = None
    max_duration_sec: Optional[int] = None
    import_mode: Optional[ImportMode] = None
    default_group_id: Optional[UUID] = None
    default_tags: Optional[list[str]] = None
    default_template_id: Optional[UUID] = None
    default_location: Optional[str] = None
    default_owner: Optional[str] = None
    enable_monitoring: Optional[bool] = None
    keep_disabled: Optional[bool] = None
    notify_recipients: Optional[list[str]] = None


class DiscoveryProfileResponse(DiscoveryProfileBase):
    id: UUID
    last_run_id: Optional[UUID] = None
    created_by: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime

    # Summary fields derived in service
    last_run_status: Optional[str] = None
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    total_devices_found: int = 0
    new_devices_found: int = 0
    existing_devices_matched: int = 0
    failed_targets: int = 0
    schedule_id: Optional[UUID] = None
    schedule_summary: Optional[str] = None

    class Config:
        from_attributes = True


# ─────────────── Schedule ───────────────
class DiscoveryScheduleBase(BaseModel):
    enabled: bool = True
    schedule_type: ScheduleType
    frequency: Optional[Frequency] = None
    cron_expression: Optional[str] = None
    interval_minutes: Optional[int] = None
    time_of_day: Optional[time] = None
    day_of_week: Optional[int] = Field(default=None, ge=0, le=6)
    day_of_month: Optional[int] = Field(default=None, ge=1, le=28)
    timezone: str = "UTC"
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    maintenance_window: Optional[dict[str, Any]] = None


class DiscoveryScheduleCreate(DiscoveryScheduleBase):
    pass


class DiscoveryScheduleResponse(DiscoveryScheduleBase):
    id: UUID
    profile_id: UUID
    next_run_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    last_run_id: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ─────────────── Run ───────────────
class DiscoveryRunStartRequest(BaseModel):
    trigger_type: TriggerType = "manual"


class DiscoveryRunResponse(BaseModel):
    id: UUID
    profile_id: UUID
    profile_name: Optional[str] = None
    schedule_id: Optional[UUID] = None
    trigger_type: TriggerType
    status: RunStatus
    phase: str
    progress_pct: int

    total_targets: int
    completed_targets: int
    responding_targets: int
    failed_targets: int
    new_devices: int
    existing_devices: int
    changed_devices: int
    unknown_devices: int
    ignored_devices: int
    credential_failures: int
    duplicate_candidates: int
    ready_to_import: int

    activity_log: list[Any] = Field(default_factory=list)
    error_details: Optional[str] = None
    started_by: Optional[UUID] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ─────────────── Result ───────────────
class DiscoveryResultResponse(BaseModel):
    id: int
    run_id: UUID
    profile_id: UUID

    ip_address: str
    mac_address: Optional[str] = None
    hostname: Optional[str] = None
    fqdn: Optional[str] = None
    sys_name: Optional[str] = None
    sys_object_id: Optional[str] = None
    serial_number: Optional[str] = None

    vendor: Optional[str] = None
    device_type: Optional[str] = None
    model: Optional[str] = None
    os: Optional[str] = None
    os_version: Optional[str] = None

    protocols_detected: list[str] = Field(default_factory=list)
    open_ports: list[int] = Field(default_factory=list)
    response_time_ms: Optional[int] = None

    credential_status: CredentialStatus
    credential_used: Optional[UUID] = None

    status: ResultStatus
    matched_device_id: Optional[UUID] = None
    matched_template_id: Optional[UUID] = None
    suggested_group_id: Optional[UUID] = None
    suggested_tags: list[str] = Field(default_factory=list)
    confidence_score: int

    conflict_type: Optional[str] = None
    conflict_with_id: Optional[UUID] = None

    import_ready: bool
    imported: bool
    imported_at: Optional[datetime] = None
    imported_device_id: Optional[UUID] = None
    ignored: bool

    error_message: Optional[str] = None
    scanned_at: datetime

    class Config:
        from_attributes = True


# ─────────────── Import ───────────────
class ImportRequest(BaseModel):
    result_ids: list[int] = Field(..., min_length=1)
    group_id: Optional[UUID] = None
    template_id: Optional[UUID] = None
    snmp_credential_id: Optional[UUID] = None
    tags: list[str] = Field(default_factory=list)
    enable_monitoring: bool = True
    location: Optional[str] = None
    ping_interval: int = Field(default=60, ge=10, le=3600)
    conflict_strategy: Literal["skip", "update", "import_as_new"] = "skip"
    # Routing: "device" (network monitoring, legacy default), "server"
    # (server-monitoring inventory), "both", or "auto" — server-class results
    # (Windows/Linux hosts) become servers (+ a linked device when monitoring
    # is enabled), network gear becomes devices.
    import_as: Literal["auto", "device", "server", "both"] = "device"
    environment: Optional[str] = None  # servers.environment for server imports


class ImportResponse(BaseModel):
    batch_id: UUID
    total: int
    successful: int
    failed: int
    skipped: int
    conflicts: int
    devices_created: int = 0
    servers_created: int = 0
    items: list[dict[str, Any]]


# ─────────────── Ignore ───────────────
class IgnoreRequest(BaseModel):
    result_ids: list[int] = Field(default_factory=list)
    ip_address: Optional[str] = None
    reason: Optional[str] = None


class IgnoredDeviceResponse(BaseModel):
    id: UUID
    ip_address: Optional[str] = None
    mac_address: Optional[str] = None
    hostname: Optional[str] = None
    reason: Optional[str] = None
    ignored_at: datetime

    class Config:
        from_attributes = True


DiscoveryProfileCreate.model_rebuild()
