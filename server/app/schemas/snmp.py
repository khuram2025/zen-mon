from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field


class DiscoveryJobCreate(BaseModel):
    cidr: str = Field(..., description="CIDR range to sweep, e.g. 10.0.0.0/24")
    credential_id: Optional[UUID] = Field(default=None, description="Saved SNMP credential to use")
    community: str = Field(default="public", description="SNMPv2c community for the probe")
    snmp_version: str = Field(default="2c", pattern="^(1|2c|3)$", description="SNMP version")
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


# ---- Device Profiles CRUD ----

class OidItem(BaseModel):
    oid: str = Field(..., description="SNMP OID, e.g. 1.3.6.1.2.1.1.3.0")
    name: str = Field(..., description="Human-friendly metric name")
    type: str = Field(default="gauge", description="gauge | counter | string")
    description: str = Field(default="")

class OidGroup(BaseModel):
    name: str = Field(..., description="Group name, e.g. System, CPU, Memory")
    oids: list[OidItem] = Field(default_factory=list)

class MatchRules(BaseModel):
    sys_object_id_prefixes: list[str] = Field(default_factory=list)
    sys_descr_regex: Optional[str] = None
    default_vendor: Optional[str] = None
    default_model: Optional[str] = None
    extract_vendor: Optional[str] = None
    extract_model: Optional[str] = None
    extract_os_version: Optional[str] = None

class ProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    vendor: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = None
    match_rules: MatchRules = Field(default_factory=MatchRules)
    oid_groups: list[OidGroup] = Field(default_factory=list)

class ProfileUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    vendor: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = None
    match_rules: Optional[MatchRules] = None
    oid_groups: Optional[list[OidGroup]] = None

class ProfileResponse(BaseModel):
    id: UUID
    name: str
    vendor: Optional[str]
    version: int
    builtin: bool
    description: Optional[str]
    match_rules: dict
    oid_groups: list
    created_at: datetime
    updated_at: datetime
    device_count: int = 0

    model_config = {"from_attributes": True}
