from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field


class DiscoveryJobCreate(BaseModel):
    cidr: str = Field(..., description="CIDR range to sweep, e.g. 10.0.0.0/24")
    community: str = Field(default="public", description="SNMPv2c community for the probe")
    snmp_version: str = Field(default="2c", pattern="^(1|2c)$", description="Only v1/v2c supported in discovery for now")
    snmp_port: int = Field(default=161, ge=1, le=65535)
    timeout_ms: int = Field(default=1500, ge=200, le=10000)


class DiscoveryJobResponse(BaseModel):
    id: UUID
    cidr: str
    community: Optional[str]
    snmp_version: str
    snmp_port: int
    timeout_ms: int
    status: str
    total_hosts: int
    scanned_hosts: int
    responding_hosts: int
    error_message: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class DiscoveryResultResponse(BaseModel):
    id: int
    job_id: UUID
    ip_address: str
    is_reachable: bool
    snmp_responded: bool
    sys_object_id: Optional[str]
    sys_descr: Optional[str]
    sys_name: Optional[str]
    hostname_guess: Optional[str]
    matched_profile_id: Optional[UUID]
    matched_vendor: Optional[str]
    matched_model: Optional[str]
    matched_os_version: Optional[str]
    already_known: bool
    imported: bool
    imported_device_id: Optional[UUID]
    error_message: Optional[str]
    scanned_at: datetime

    model_config = {"from_attributes": True}


class DiscoveryImportRequest(BaseModel):
    result_ids: list[int] = Field(..., min_length=1, description="discovery_results.id values to import")
    default_group_id: Optional[UUID] = None


class DiscoveryImportResponse(BaseModel):
    created: int
    skipped: int
    errors: list[str]
    device_ids: list[UUID]


class MibUploadResponse(BaseModel):
    id: UUID
    name: str
    filename: str
    size_bytes: int
    sha256: str
    uploaded_at: datetime

    model_config = {"from_attributes": True}
