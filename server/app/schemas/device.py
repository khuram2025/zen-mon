from datetime import datetime
from typing import Literal, Optional
from uuid import UUID
from pydantic import BaseModel, Field

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


class DeviceResponse(BaseModel):
    id: UUID
    hostname: str
    ip_address: str
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
    description: Optional[str]
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
    snmp_credential_id: Optional[UUID] = None
    snmp_auth_configured: bool = False
    snmp_priv_configured: bool = False

    model_config = {"from_attributes": True}


class DeviceSummary(BaseModel):
    total: int
    up: int
    down: int
    degraded: int
    unknown: int
    maintenance: int


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
