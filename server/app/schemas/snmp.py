from datetime import datetime
from typing import Optional
from uuid import UUID
from pydantic import BaseModel, Field, field_validator


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


# ---- Device Profiles (Monitoring Templates) CRUD ----
#
# The oid_groups JSON shape is shared with the poller (oidgroups.go) and the
# migrate-062 builtin seeds. Poller-side fields: key/oid/type/unit/scale/
# value_map. UI/alerting-side fields: labels (enum code -> {text, sev}) and
# thresholds ({warn, crit, op}).

_KEY_PATTERN = r"^[a-z0-9][a-z0-9_]{0,62}$"
_OID_PATTERN = r"^\.?\d+(\.\d+)+$"


class MetricThresholds(BaseModel):
    warn: Optional[float] = None
    crit: Optional[float] = None
    op: str = Field(default=">=", description=">= | <=")


class MetricLabel(BaseModel):
    text: str
    sev: str = Field(default="info", description="ok | warn | crit | info")


class OidMetric(BaseModel):
    key: str = Field(..., pattern=_KEY_PATTERN,
                     description="Metric key, unique within the template (series = tpl_<key>)")
    name: str = Field(..., min_length=1, max_length=120)
    oid: str = Field(..., pattern=_OID_PATTERN)
    type: str = Field(default="gauge", description="gauge | counter | enum | string")
    unit: Optional[str] = Field(default=None, max_length=32)
    scale: Optional[float] = None
    value_map: Optional[dict[str, float]] = Field(
        default=None, description="Coerce string agent values to numeric codes")
    labels: Optional[dict[str, MetricLabel]] = Field(
        default=None, description="Enum code -> display label + severity")
    thresholds: Optional[MetricThresholds] = None

    @field_validator("type")
    @classmethod
    def _type_ok(cls, v: str) -> str:
        if v not in ("gauge", "counter", "enum", "string"):
            raise ValueError("type must be gauge|counter|enum|string")
        return v


class OidGroupTable(BaseModel):
    label_oid: Optional[str] = Field(default=None, pattern=_OID_PATTERN,
                                     description="Column walked for per-row labels")


class OidGroupChildren(BaseModel):
    """Declares that rows of a table group are controller-managed devices
    (FortiGate's FortiAPs/FortiSwitches, a WLC's thin APs). The managed-device
    sync service materializes each row as a child device of the polled
    controller when the controller has promote_managed enabled."""
    device_type: str = Field(default="access_point")
    vendor: Optional[str] = Field(default=None, max_length=100)
    status_key: Optional[str] = Field(
        default=None, pattern=_KEY_PATTERN,
        description="Metric key whose enum code drives the child's status")
    status_map: dict[str, str] = Field(
        default_factory=dict,
        description="Enum code -> up|down|degraded; unmapped codes -> unknown")
    model_key: Optional[str] = Field(default=None, pattern=_KEY_PATTERN)
    os_version_key: Optional[str] = Field(default=None, pattern=_KEY_PATTERN)
    serial_key: Optional[str] = Field(default=None, pattern=_KEY_PATTERN)
    ip_key: Optional[str] = Field(default=None, pattern=_KEY_PATTERN)

    @field_validator("device_type")
    @classmethod
    def _dtype_ok(cls, v: str) -> str:
        allowed = ("router", "switch", "firewall", "server", "access_point", "printer", "other")
        if v not in allowed:
            raise ValueError(f"device_type must be one of {allowed}")
        return v

    @field_validator("status_map")
    @classmethod
    def _map_ok(cls, v: dict[str, str]) -> dict[str, str]:
        for code, status in v.items():
            if status not in ("up", "down", "degraded"):
                raise ValueError(f"status_map[{code}] must be up|down|degraded")
        return v


class OidGroup(BaseModel):
    key: str = Field(..., pattern=_KEY_PATTERN)
    name: str = Field(..., min_length=1, max_length=120)
    kind: str = Field(default="scalar", description="scalar | table")
    description: Optional[str] = None
    table: Optional[OidGroupTable] = None
    metrics: list[OidMetric] = Field(default_factory=list)
    children: Optional[OidGroupChildren] = None

    @field_validator("kind")
    @classmethod
    def _kind_ok(cls, v: str) -> str:
        if v not in ("scalar", "table"):
            raise ValueError("kind must be scalar|table")
        return v

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
