from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field


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
