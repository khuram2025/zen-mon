import re
from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from pydantic import BaseModel, Field, field_validator, field_serializer


class DiscoveryJobCreate(BaseModel):
    cidr: str = Field(..., description="CIDR range to sweep, e.g. 10.0.0.0/24")
    credential_id: Optional[UUID] = Field(default=None, description="Saved SNMP credential to use")
    community: str = Field(default="public", description="SNMPv2c community for the probe")
    snmp_version: str = Field(default="2c", pattern="^(1|2c|3)$", description="SNMP version")
    snmp_port: int = Field(default=161, ge=1, le=65535)
    timeout_ms: int = Field(default=1500, ge=200, le=10000)


class DiscoveryJobResponse(BaseModel):
    @field_serializer("community")
    def mask_community(self, value):
        return None

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
    format: Optional[str] = Field(
        default=None, description='"hex": keep a binary OctetString (InetAddress, PhysAddress) as hex')

    @field_validator("format")
    @classmethod
    def _format_ok(cls, v: Optional[str]) -> Optional[str]:
        if v not in (None, "hex"):
            raise ValueError("format must be hex")
        return v

    @field_validator("type")
    @classmethod
    def _type_ok(cls, v: str) -> str:
        if v not in ("gauge", "counter", "enum", "string"):
            raise ValueError("type must be gauge|counter|enum|string")
        return v


class OidGroupTable(BaseModel):
    label_oid: Optional[str] = Field(default=None, pattern=_OID_PATTERN,
                                     description="Column walked for per-row labels")
    max_rows: Optional[int] = Field(default=None, ge=1, le=5000,
                                    description="Per-poll row cap (default 500)")


class OidGroupBelongsTo(BaseModel):
    """How a controller-source row belongs to a managed child: by a serial
    column, by raw index prefix, or through the AP profile name."""
    group: str = Field(..., pattern=_KEY_PATTERN)
    by: str
    metric: Optional[str] = Field(default=None, pattern=_KEY_PATTERN)
    layout: list[str] = Field(default_factory=list)

    @field_validator("by")
    @classmethod
    def _by_ok(cls, v: str) -> str:
        if v not in ("serial", "index_prefix", "profile"):
            raise ValueError("by must be serial|index_prefix|profile")
        return v

    @field_validator("layout")
    @classmethod
    def _layout_ok(cls, v: list[str]) -> list[str]:
        if any(part not in ("int", "str", "mac") for part in v):
            raise ValueError("layout parts must be int|str|mac")
        return v


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
    # Controller-derived readings shown on the child (migrate-182).
    mac_key: Optional[str] = Field(default=None, pattern=_KEY_PATTERN)
    cpu_key: Optional[str] = Field(default=None, pattern=_KEY_PATTERN)
    memory_key: Optional[str] = Field(default=None, pattern=_KEY_PATTERN)
    uptime_key: Optional[str] = Field(default=None, pattern=_KEY_PATTERN,
                                      description="Seconds since the child booted")
    clients_key: Optional[str] = Field(default=None, pattern=_KEY_PATTERN)
    state_key: Optional[str] = Field(default=None, pattern=_KEY_PATTERN)
    auth_key: Optional[str] = Field(default=None, pattern=_KEY_PATTERN)
    serial_from_index: Optional[dict[str, Any]] = Field(
        default=None, description='{"layout": ["int","str"], "part": 1}: serial decoded from the row index')

    @field_validator("device_type")
    @classmethod
    def _dtype_ok(cls, v: str) -> str:
        # A key of the device type catalogue (device_types, migrate-189); the
        # template service checks that it exists when the template is saved.
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,49}", v or ""):
            raise ValueError("device_type must be a device type key (lowercase letters, digits, _)")
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
    interval_seconds: int = Field(default=0, ge=0, le=86400)
    kind: str = Field(default="scalar", description="scalar | table")
    description: Optional[str] = None
    table: Optional[OidGroupTable] = None
    metrics: list[OidMetric] = Field(default_factory=list)
    children: Optional[OidGroupChildren] = None
    display: Optional[str] = Field(
        default=None, description='"children": rows are shown on managed children, not on the polled device')
    belongs_to: Optional[OidGroupBelongsTo] = None
    tab: Optional[str] = Field(default=None, max_length=60)
    applies_when: Optional[dict[str, Any]] = None

    @field_validator("kind")
    @classmethod
    def _kind_ok(cls, v: str) -> str:
        if v not in ("scalar", "table"):
            raise ValueError("kind must be scalar|table")
        return v

class MatchRules(BaseModel):
    """How a device is matched to a template (contract: docs/device-templates.md)."""
    sys_object_id_prefixes: list[str] = Field(default_factory=list, max_length=100)
    sys_descr_regex: Optional[str] = None
    require_sys_descr: bool = Field(
        default=False, description="A sysObjectID prefix only counts when sys_descr_regex also matches")
    default_vendor: Optional[str] = Field(default=None, max_length=100)
    default_model: Optional[str] = Field(default=None, max_length=255)
    extract_vendor: Optional[str] = None
    extract_model: Optional[str] = None
    extract_os_version: Optional[str] = None

    @field_validator("sys_object_id_prefixes")
    @classmethod
    def _prefixes_ok(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for raw in v:
            oid = (raw or "").strip().lstrip(".")
            if not oid:
                continue
            if not re.fullmatch(r"\d+(\.\d+)*", oid):
                raise ValueError(f"'{raw}' is not a numeric OID")
            if oid not in out:
                out.append(oid)
        return out

    @field_validator("sys_descr_regex", "extract_vendor", "extract_model", "extract_os_version")
    @classmethod
    def _regex_ok(cls, v: Optional[str]) -> Optional[str]:
        from app.services.device_templates.classify import regex_problem
        if v is None or v == "":
            return None
        problem = regex_problem(v)
        if problem:
            raise ValueError(problem)
        return v

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
    # Device Templates (migrate-189)
    builtin_key: Optional[str] = None
    parent_id: Optional[UUID] = None
    parent_name: Optional[str] = None
    device_type: Optional[str] = None
    icon: Optional[str] = None
    match_priority: int = 100
    revision: int = 1
    has_draft: bool = False
    draft_updated_at: Optional[datetime] = None
    pilot_device_count: int = 0
    pilot_started_at: Optional[datetime] = None
    suggestion_count: int = 0
    customized_count: int = 0

    model_config = {"from_attributes": True}


class TemplateContent(BaseModel):
    """Everything a template publishes. For a customised template (one with a
    parent) ``oid_groups`` are the edited effective groups; they are stored as
    differences from the parent (``overlay``)."""
    name: str = Field(..., min_length=1, max_length=255)
    vendor: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = Field(default=None, max_length=4000)
    device_type: Optional[str] = Field(default=None, max_length=50)
    icon: Optional[str] = Field(default=None, max_length=40)
    match_priority: int = Field(default=100, ge=0, le=1000)
    match_rules: MatchRules = Field(default_factory=MatchRules)
    oid_groups: list[OidGroup] = Field(default_factory=list, max_length=200)


class TemplatePublishRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)
    pilot_device_ids: Optional[list[UUID]] = Field(default=None, max_length=50)


class TemplateRollbackRequest(BaseModel):
    revision: int = Field(..., ge=1)
    reason: Optional[str] = Field(default=None, max_length=500)


class TemplateCustomizeRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    move_devices: bool = Field(default=True, description="Move the parent's devices to the customised template")


class TemplateAssignRequest(BaseModel):
    device_ids: list[UUID] = Field(..., max_length=5000)
    set_device_type: bool = Field(default=False, description="Also set the template's device type on the devices")


class TemplateTestRequest(BaseModel):
    device_id: UUID
    source: str = Field(default="draft", description="draft | published")
    group_keys: Optional[list[str]] = Field(default=None, max_length=50)

    @field_validator("source")
    @classmethod
    def _source_ok(cls, v: str) -> str:
        if v not in ("draft", "published"):
            raise ValueError("source must be draft|published")
        return v


class TemplateClassifyRequest(BaseModel):
    device_id: Optional[UUID] = None
    sys_object_id: Optional[str] = Field(default=None, max_length=255)
    sys_descr: Optional[str] = Field(default=None, max_length=4000)


# Device types seeded by migrate-189. Built-in types cannot be deleted, so
# these keys always exist and need no catalogue lookup to validate.
BUILTIN_DEVICE_TYPES = frozenset({
    "router", "switch", "firewall", "load_balancer", "sdwan_edge", "wireless_controller", "access_point",
    "server", "storage", "ups", "pdu", "printer", "ip_camera", "voip", "other",
})

DEVICE_TYPE_CATEGORIES = ("network", "security", "wireless", "server", "power", "storage", "endpoint", "other")


class DeviceTypeCreate(BaseModel):
    key: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,49}$")
    label: str = Field(..., min_length=1, max_length=80)
    category: str = "other"
    icon: str = Field(default="other", max_length=40)
    sort_order: int = Field(default=500, ge=0, le=10000)
    description: Optional[str] = Field(default=None, max_length=500)

    @field_validator("category")
    @classmethod
    def _cat_ok(cls, v: str) -> str:
        if v not in DEVICE_TYPE_CATEGORIES:
            raise ValueError(f"category must be one of {DEVICE_TYPE_CATEGORIES}")
        return v


class DeviceTypeUpdate(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=80)
    category: Optional[str] = None
    icon: Optional[str] = Field(default=None, max_length=40)
    sort_order: Optional[int] = Field(default=None, ge=0, le=10000)
    description: Optional[str] = Field(default=None, max_length=500)

    @field_validator("category")
    @classmethod
    def _cat_ok(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in DEVICE_TYPE_CATEGORIES:
            raise ValueError(f"category must be one of {DEVICE_TYPE_CATEGORIES}")
        return v
