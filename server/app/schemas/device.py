from datetime import date, datetime, time
from typing import Literal, Optional
from uuid import UUID
from pydantic import BaseModel, Field, field_serializer, model_validator

SnmpVersion = Literal["1", "2c", "3"]
SnmpAuthProtocol = Literal["MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512"]
SnmpPrivProtocol = Literal["DES", "3DES", "AES", "AES128", "AES192", "AES256"]


class SnmpConfig(BaseModel):
    """SNMP configuration subset — reused in DeviceCreate/Update.

    Passphrases are write-only: accepted on create/update, never returned.
    """

    snmp_enabled: bool = False
    snmp_version: SnmpVersion = "2c"
    snmp_port: int = Field(default=161, ge=1, le=65535)
    snmp_community: Optional[str] = None
    snmp_v3_username: Optional[str] = None
    snmp_v3_context: Optional[str] = None
    snmp_auth_protocol: Optional[SnmpAuthProtocol] = None
    snmp_auth_passphrase: Optional[str] = Field(default=None, repr=False)
    snmp_priv_protocol: Optional[SnmpPrivProtocol] = None
    snmp_priv_passphrase: Optional[str] = Field(default=None, repr=False)
    snmp_timeout_ms: int = Field(default=2000, ge=100, le=60000)
    snmp_retries: int = Field(default=2, ge=0, le=10)
    snmp_max_repetitions: int = Field(default=25, ge=1, le=200)
    snmp_poll_interval: int = Field(default=60, ge=30, le=3600)
    profile_id: Optional[UUID] = None


class DeviceCreate(BaseModel):
    hostname: str = Field(..., max_length=255)
    ip_address: str = Field(..., max_length=45)
    device_type: str = Field(default="other")
    location: Optional[str] = None
    group_id: Optional[UUID] = None
    tags: list[str] = Field(default_factory=list)
    ping_enabled: bool = True
    ping_interval: int = Field(default=60, ge=10, le=3600)
    description: Optional[str] = None

    # SNMP
    snmp_enabled: bool = False
    snmp_version: SnmpVersion = "2c"
    snmp_port: int = Field(default=161, ge=1, le=65535)
    snmp_community: Optional[str] = None
    snmp_v3_username: Optional[str] = None
    snmp_v3_context: Optional[str] = None
    snmp_auth_protocol: Optional[SnmpAuthProtocol] = None
    snmp_auth_passphrase: Optional[str] = Field(default=None, repr=False)
    snmp_priv_protocol: Optional[SnmpPrivProtocol] = None
    snmp_priv_passphrase: Optional[str] = Field(default=None, repr=False)
    snmp_timeout_ms: int = Field(default=2000, ge=100, le=60000)
    snmp_retries: int = Field(default=2, ge=0, le=10)
    snmp_max_repetitions: int = Field(default=25, ge=1, le=200)
    snmp_poll_interval: int = Field(default=60, ge=30, le=3600)
    profile_id: Optional[UUID] = None
    snmp_credential_id: Optional[UUID] = None


class DeviceUpdate(BaseModel):
    hostname: Optional[str] = None
    ip_address: Optional[str] = None
    device_type: Optional[str] = None
    location: Optional[str] = None
    group_id: Optional[UUID] = None
    tags: Optional[list[str]] = None
    ping_enabled: Optional[bool] = None
    ping_interval: Optional[int] = Field(default=None, ge=10, le=3600)
    description: Optional[str] = None
    # Device page "Owner and notes" (migrate-165). Blank clears.
    owner: Optional[str] = Field(default=None, max_length=255)
    notes: Optional[str] = Field(default=None, max_length=20000)

    # SNMP — all optional
    snmp_enabled: Optional[bool] = None
    snmp_version: Optional[SnmpVersion] = None
    snmp_port: Optional[int] = Field(default=None, ge=1, le=65535)
    snmp_community: Optional[str] = None
    snmp_v3_username: Optional[str] = None
    snmp_v3_context: Optional[str] = None
    snmp_auth_protocol: Optional[SnmpAuthProtocol] = None
    snmp_auth_passphrase: Optional[str] = Field(default=None, repr=False)
    snmp_priv_protocol: Optional[SnmpPrivProtocol] = None
    snmp_priv_passphrase: Optional[str] = Field(default=None, repr=False)
    snmp_timeout_ms: Optional[int] = Field(default=None, ge=100, le=60000)
    snmp_retries: Optional[int] = Field(default=None, ge=0, le=10)
    snmp_max_repetitions: Optional[int] = Field(default=None, ge=1, le=200)
    snmp_poll_interval: Optional[int] = Field(default=None, ge=30, le=3600)
    profile_id: Optional[UUID] = None
    snmp_credential_id: Optional[UUID] = None
    # Controller opt-in: materialize template-reported managed devices
    # (APs, FortiSwitches) as child devices of this one.
    promote_managed: Optional[bool] = None


class DeviceResponse(BaseModel):
    @field_serializer('snmp_community')
    def mask_community(self, value):
        return None

    id: UUID
    hostname: str
    # None for controller-reported children that expose no reachable IP.
    ip_address: Optional[str]
    device_type: str
    location: Optional[str]
    group_id: Optional[UUID]
    group_name: Optional[str] = None
    tags: list
    ping_enabled: bool
    ping_interval: int
    status: str
    last_seen: Optional[datetime]
    last_rtt_ms: Optional[float]
    # Confirmed status (migrate-161): flapping overlays the status, it never
    # replaces it. flap_kind is reachability | performance | both while
    # flapping; status_changed_at is the last hard change (None until the
    # first one after the upgrade).
    is_flapping: bool = False
    flapping_since: Optional[datetime] = None
    flap_kind: Optional[str] = None
    status_changed_at: Optional[datetime] = None
    description: Optional[str]
    owner: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    # SNMP — never return passphrases
    snmp_enabled: bool = False
    snmp_version: Optional[str] = None
    snmp_port: Optional[int] = None
    snmp_community: Optional[str] = None
    snmp_v3_username: Optional[str] = None
    snmp_v3_context: Optional[str] = None
    snmp_auth_protocol: Optional[str] = None
    snmp_priv_protocol: Optional[str] = None
    snmp_timeout_ms: Optional[int] = None
    snmp_retries: Optional[int] = None
    snmp_max_repetitions: Optional[int] = None
    snmp_poll_interval: Optional[int] = None
    sys_object_id: Optional[str] = None
    vendor: Optional[str] = None
    model: Optional[str] = None
    os_version: Optional[str] = None
    profile_id: Optional[UUID] = None
    profile_name: Optional[str] = None
    profile_source: Optional[str] = None
    profile_suggested_id: Optional[UUID] = None
    snmp_credential_id: Optional[UUID] = None
    snmp_auth_configured: bool = False
    snmp_priv_configured: bool = False

    # Controller-managed children (migrate-069)
    poll_mode: str = "direct"
    managed_by_device_id: Optional[UUID] = None
    managed_by_hostname: Optional[str] = None
    serial_number: Optional[str] = None
    firmware_version: Optional[str] = None
    managed_ip: Optional[str] = None
    managed_source: Optional[str] = None
    managed_last_seen: Optional[datetime] = None
    promote_managed: bool = False
    managed_children_count: int = 0

    model_config = {"from_attributes": True}


class DeviceSummary(BaseModel):
    total: int
    up: int
    down: int
    degraded: int
    unknown: int
    maintenance: int
    # Devices flagged as flapping, whatever their status (an overlay count:
    # these devices are also counted in their status bucket).
    flapping: int = 0


class DeviceGroupResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str]
    color: Optional[str]
    device_count: int = 0

    model_config = {"from_attributes": True}


class DeviceBulkImportItem(BaseModel):
    hostname: str = Field(..., max_length=255)
    ip_address: str = Field(..., max_length=45)
    device_type: str = Field(default="other")
    location: Optional[str] = None
    group_name: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    ping_enabled: bool = True
    ping_interval: int = Field(default=60, ge=10, le=3600)
    description: Optional[str] = None


class BulkImportRequest(BaseModel):
    devices: list[DeviceBulkImportItem]


class BulkImportResult(BaseModel):
    total: int
    created: int
    skipped: int
    errors: list[str]


class DeviceExportItem(BaseModel):
    hostname: str
    ip_address: str
    device_type: str
    location: Optional[str]
    group_name: Optional[str]
    tags: list[str]
    ping_enabled: bool
    ping_interval: int
    status: str
    last_rtt_ms: Optional[float]
    description: Optional[str]


class MaintenanceRecurrence(BaseModel):
    """A weekly recurrence (migrate-207): an occurrence starts on each chosen
    weekday (ISO 1 = Monday .. 7 = Sunday) at ``start_time`` local time in
    ``timezone`` (default: the organisation's) and lasts ``duration_minutes``
    (1 minute to 7 days); ``until`` is the last local date one may start."""
    days: list[int] = Field(min_length=1, max_length=7)
    start_time: time
    duration_minutes: int = Field(ge=1, le=7 * 24 * 60)
    timezone: Optional[str] = Field(default=None, max_length=64)
    until: Optional[date] = None

    @model_validator(mode="after")
    def _validate(self) -> "MaintenanceRecurrence":
        if any(d < 1 or d > 7 for d in self.days):
            raise ValueError("days must be ISO weekdays 1 (Monday) to 7 (Sunday)")
        self.days = sorted(set(self.days))
        self.start_time = self.start_time.replace(second=0, microsecond=0, tzinfo=None)
        if self.timezone is not None:
            from app.services.maintenance_windows import valid_timezone
            self.timezone = self.timezone.strip() or None
            if self.timezone is not None and not valid_timezone(self.timezone):
                raise ValueError(f"unknown time zone {self.timezone!r}")
        return self


class DeviceMaintenanceCreate(BaseModel):
    """A one-off window (``starts_at`` + ``ends_at``, unchanged) or a
    recurring one (``recurrence``; ``starts_at`` then optionally says from when
    occurrences count, default now, and ``ends_at`` is not given)."""
    scope_type: Literal["device", "group", "tag", "all", "server", "server_group", "server_tag"]
    scope_device_id: Optional[UUID] = None
    scope_group_id: Optional[UUID] = None
    scope_server_id: Optional[UUID] = None
    scope_tag: Optional[str] = Field(default=None, max_length=120)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    recurrence: Optional[MaintenanceRecurrence] = None
    reason: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _validate(self) -> "DeviceMaintenanceCreate":
        if self.recurrence is None:
            if self.starts_at is None or self.ends_at is None:
                raise ValueError("starts_at and ends_at are required for a one-off window")
            if self.ends_at <= self.starts_at:
                raise ValueError("ends_at must be after starts_at")
        elif self.ends_at is not None:
            raise ValueError("a recurring window takes recurrence, not ends_at")
        if self.scope_type == "device" and not self.scope_device_id:
            raise ValueError("scope_device_id is required for scope_type=device")
        if self.scope_type in ("group", "server_group") and not self.scope_group_id:
            raise ValueError(f"scope_group_id is required for scope_type={self.scope_type}")
        if self.scope_type == "server" and not self.scope_server_id:
            raise ValueError("scope_server_id is required for scope_type=server")
        if self.scope_type in ("tag", "server_tag") and not (self.scope_tag or "").strip():
            raise ValueError(f"scope_tag is required for scope_type={self.scope_type}")
        return self


class MaintenanceRecurrenceResponse(BaseModel):
    days: list[int]
    start_time: str
    duration_minutes: int
    timezone: str
    until: Optional[str] = None


class DeviceMaintenanceResponse(BaseModel):
    id: UUID
    scope_type: str
    scope_device_id: Optional[UUID] = None
    scope_group_id: Optional[UUID] = None
    scope_server_id: Optional[UUID] = None
    scope_tag: Optional[str] = None
    scope_label: str
    # One-off: the window. Recurring: its first occurrence.
    starts_at: datetime
    ends_at: datetime
    reason: Optional[str] = None
    created_by: Optional[UUID] = None
    created_at: datetime
    active: bool
    recurring: bool = False
    recurrence: Optional[MaintenanceRecurrenceResponse] = None
    schedule: str = "One-off"
    # The occurrence in force now, else the next one; None when it is over.
    next_starts_at: Optional[datetime] = None
    next_ends_at: Optional[datetime] = None
