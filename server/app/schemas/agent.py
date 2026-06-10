"""Pydantic schemas for the server-monitoring agent API.

Two surfaces:
  * Admin / dashboard endpoints (/api/v1/servers, /api/v1/agent-policies,
    /api/v1/agent-fleet) — JWT auth.
  * Agent-facing endpoints (/api/v1/agents/*) — bearer api-key auth after
    enrollment.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# ── Common ───────────────────────────────────────────────────────────

OsType = Literal["windows", "linux", "macos", "bsd", "other", "unknown"]
CollectionMode = Literal["agent", "agentless_wmi", "agentless_winrm", "snmp", "ssh", "none"]
ServerStatus = Literal["healthy", "warning", "critical", "unknown", "stale", "disabled"]
AgentStatus = Literal["enrolling", "online", "stale", "offline", "disabled", "updating", "error"]
AgentPlatform = Literal["windows", "linux", "macos", "other"]
UpdateRing = Literal["canary", "beta", "stable", "pinned"]


# ── Servers (admin) ──────────────────────────────────────────────────

class ServerCreate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=255)
    hostname: Optional[str] = None
    fqdn: Optional[str] = None
    primary_ip: Optional[str] = None
    site_id: Optional[UUID] = None
    device_id: Optional[UUID] = None
    os_type: OsType = "unknown"
    collection_mode: CollectionMode = "agent"
    environment: Optional[str] = None
    owner: Optional[str] = None
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class ServerUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    hostname: Optional[str] = None
    fqdn: Optional[str] = None
    primary_ip: Optional[str] = None
    site_id: Optional[UUID] = None
    device_id: Optional[UUID] = None
    os_type: Optional[OsType] = None
    collection_mode: Optional[CollectionMode] = None
    status: Optional[ServerStatus] = None
    environment: Optional[str] = None
    owner: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None


class ServerResponse(BaseModel):
    id: str
    display_name: str
    hostname: Optional[str]
    fqdn: Optional[str]
    primary_ip: Optional[str]
    site_id: Optional[str]
    site_name: Optional[str] = None
    device_id: Optional[str]
    os_type: str
    os_name: Optional[str]
    os_version: Optional[str]
    kernel_or_build: Optional[str]
    architecture: Optional[str]
    collection_mode: str
    status: str
    environment: Optional[str]
    owner: Optional[str]
    tags: List[str] = Field(default_factory=list)
    last_seen: Optional[datetime]
    description: Optional[str]
    status_reasons: List[str] = Field(default_factory=list)
    agent_id: Optional[str] = None
    agent_status: Optional[str] = None
    agent_version: Optional[str] = None
    agent_last_heartbeat_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class ServerBulkAction(BaseModel):
    server_ids: List[UUID] = Field(default_factory=list)
    action: Literal["add_tags", "remove_tags", "set_environment", "decommission", "delete"]
    tags: List[str] = Field(default_factory=list)
    environment: Optional[str] = None


# ── Agent policies ───────────────────────────────────────────────────

class AgentPolicyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    platform: Literal["windows", "linux", "any"] = "windows"
    metric_interval_s: int = Field(30, ge=5, le=3600)
    upload_interval_s: int = Field(60, ge=5, le=3600)
    process_top_n: int = Field(25, ge=0, le=500)
    service_watchlist: List[str] = Field(default_factory=list)
    process_watchlist: List[str] = Field(default_factory=list)
    event_log_filters: List[Dict[str, Any]] = Field(default_factory=list)
    disk_ignore: List[str] = Field(default_factory=list)
    network_ignore: List[str] = Field(default_factory=list)
    cardinality_limits: Dict[str, Any] = Field(default_factory=dict)
    update_ring: UpdateRing = "stable"
    feature_flags: Dict[str, Any] = Field(default_factory=dict)


class AgentPolicyUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    platform: Optional[Literal["windows", "linux", "any"]] = None
    metric_interval_s: Optional[int] = Field(default=None, ge=5, le=3600)
    upload_interval_s: Optional[int] = Field(default=None, ge=5, le=3600)
    process_top_n: Optional[int] = Field(default=None, ge=0, le=500)
    service_watchlist: Optional[List[str]] = None
    process_watchlist: Optional[List[str]] = None
    event_log_filters: Optional[List[Dict[str, Any]]] = None
    disk_ignore: Optional[List[str]] = None
    network_ignore: Optional[List[str]] = None
    cardinality_limits: Optional[Dict[str, Any]] = None
    update_ring: Optional[UpdateRing] = None
    feature_flags: Optional[Dict[str, Any]] = None


class AgentPolicyResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    platform: str
    metric_interval_s: int
    upload_interval_s: int
    process_top_n: int
    service_watchlist: List[str]
    process_watchlist: List[str]
    event_log_filters: List[Dict[str, Any]]
    disk_ignore: List[str]
    network_ignore: List[str]
    cardinality_limits: Dict[str, Any]
    update_ring: str
    feature_flags: Dict[str, Any]
    config_version: int
    is_builtin: bool
    agent_count: int = 0
    created_at: datetime
    updated_at: datetime


# ── Enrollment tokens ───────────────────────────────────────────────

class InstallTokenCreate(BaseModel):
    platform: Literal["windows", "linux", "macos", "any"] = "windows"
    site_id: Optional[UUID] = None
    policy_id: Optional[UUID] = None
    server_id: Optional[UUID] = None
    hostname_hint: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    ttl_hours: int = Field(24, ge=1, le=720)
    max_uses: int = Field(1, ge=1, le=100)


class InstallTokenResponse(BaseModel):
    token_id: str
    enrollment_token: str
    token_prefix: str
    expires_at: datetime
    max_uses: int
    server_url: str
    platform: str
    site_id: Optional[str] = None
    policy_id: Optional[str] = None
    install_command: str
    msi_download_url: Optional[str] = None


# ── Agents (admin) ───────────────────────────────────────────────────

class AgentResponse(BaseModel):
    id: str
    server_id: Optional[str]
    server_name: Optional[str] = None
    site_id: Optional[str]
    site_name: Optional[str] = None
    agent_uid: str
    hostname: Optional[str]
    platform: str
    version: Optional[str]
    status: str
    api_key_prefix: Optional[str]
    last_heartbeat_at: Optional[datetime]
    last_metric_at: Optional[datetime]
    last_config_hash: Optional[str]
    queue_depth: int
    spool_bytes: int
    update_ring: str
    desired_version: Optional[str]
    current_version: Optional[str]
    certificate_expires_at: Optional[datetime]
    last_ip: Optional[str]
    policy_id: Optional[str]
    policy_name: Optional[str] = None
    config_apply_error: Optional[str]
    tags: List[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class AgentBulkAction(BaseModel):
    agent_ids: List[UUID] = Field(default_factory=list)
    action: Literal["change_policy", "change_update_ring", "request_diagnostics",
                    "rotate_certificate", "trigger_upgrade", "disable", "enable"]
    policy_id: Optional[UUID] = None
    update_ring: Optional[UpdateRing] = None
    target_version: Optional[str] = None


# ── Agent-facing API ─────────────────────────────────────────────────

class AgentEnrollRequest(BaseModel):
    enrollment_token: str
    agent_uid: str = Field(..., min_length=8, max_length=128)
    hostname: str
    platform: AgentPlatform = "windows"
    fqdn: Optional[str] = None
    primary_ip: Optional[str] = None
    os_name: Optional[str] = None
    os_version: Optional[str] = None
    kernel_or_build: Optional[str] = None
    architecture: Optional[str] = None
    version: str
    install_id: Optional[str] = None


class AgentEnrollResponse(BaseModel):
    agent_id: str
    server_id: str
    api_key: str
    heartbeat_interval_s: int = 30
    config_poll_interval_s: int = 60
    upload_interval_s: int = 60
    policy_id: Optional[str] = None


class AgentHeartbeatRequest(BaseModel):
    version: str
    uptime_seconds: Optional[int] = None
    queue_depth: int = 0
    spool_bytes: int = 0
    config_hash: Optional[str] = None
    config_apply_error: Optional[str] = None


class AgentHeartbeatResponse(BaseModel):
    ok: bool = True
    server_time: datetime
    config_etag: Optional[str] = None
    has_commands: bool = False
    desired_version: Optional[str] = None
    backpressure: Optional[Dict[str, Any]] = None


class AgentConfigResponse(BaseModel):
    config_version: int
    policy_id: Optional[str]
    etag: str
    metric_interval_s: int
    upload_interval_s: int
    process_top_n: int
    service_watchlist: List[str]
    process_watchlist: List[str]
    event_log_filters: List[Dict[str, Any]]
    disk_ignore: List[str]
    network_ignore: List[str]
    cardinality_limits: Dict[str, Any]
    feature_flags: Dict[str, Any]
    update_ring: str
    signature: Optional[str] = None
    signed_at: datetime


class MetricSample(BaseModel):
    """Generic batched metric envelope."""
    kind: Literal["cpu", "memory", "filesystem", "disk_io", "network",
                  "process", "service_state", "event_log", "agent_health", "inventory"]
    timestamp: datetime
    data: Dict[str, Any] = Field(default_factory=dict)


class AgentResultsBatch(BaseModel):
    agent_id: str
    server_id: str
    batch_id: str
    sequence_start: int
    sequence_end: int
    config_hash: Optional[str] = None
    agent_version: str
    collected_at: datetime
    sent_at: datetime
    metrics: List[MetricSample] = Field(default_factory=list)
    inventory: Dict[str, Any] = Field(default_factory=dict)
    events: List[Dict[str, Any]] = Field(default_factory=list)


class AgentResultsResponse(BaseModel):
    ok: bool = True
    accepted: int = 0
    rejected: int = 0
    duplicates: int = 0
    backpressure: Optional[Dict[str, Any]] = None
    errors: List[str] = Field(default_factory=list)


class AgentEventsBatch(BaseModel):
    agent_id: str
    events: List[Dict[str, Any]] = Field(default_factory=list)


class AgentDiagnosticsUpload(BaseModel):
    agent_id: str
    file_name: str
    file_size: int
    sha256: str
    diagnostic_id: Optional[str] = None
    notes: Optional[str] = None


class AgentPackagesManifest(BaseModel):
    platform: str
    channel: str
    latest_version: str
    file_name: str
    file_size: int
    sha256: str
    signature: Optional[str] = None
    released_at: datetime
    download_url: str


class AgentCommandPoll(BaseModel):
    has_commands: bool
    commands: List[Dict[str, Any]] = Field(default_factory=list)


class AgentCommandResult(BaseModel):
    success: bool
    output: Dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None


# ── Software baselines (compliance) ──────────────────────────────────

BaselineRuleType = Literal["required", "prohibited"]
BaselineMatchType = Literal["exact", "contains", "regex"]
AlertSeverity = Literal["info", "warning", "critical"]


class BaselineRuleCreate(BaseModel):
    rule_type: BaselineRuleType = "required"
    package_match: str = Field(..., min_length=1, max_length=255)
    match_type: BaselineMatchType = "contains"
    min_version: Optional[str] = Field(default=None, max_length=128)
    severity: AlertSeverity = "warning"
    notes: Optional[str] = None


class BaselineRuleResponse(BaselineRuleCreate):
    id: str
    baseline_id: str
    created_at: datetime


class BaselineCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: bool = True
    os_type: Optional[Literal["windows", "linux", "macos", "bsd", "other"]] = None
    site_id: Optional[UUID] = None
    match_tags: List[str] = Field(default_factory=list)
    alerting: bool = True
    rules: List[BaselineRuleCreate] = Field(default_factory=list)


class BaselineUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    enabled: Optional[bool] = None
    os_type: Optional[Literal["windows", "linux", "macos", "bsd", "other"]] = None
    clear_os_type: bool = False
    site_id: Optional[UUID] = None
    clear_site: bool = False
    match_tags: Optional[List[str]] = None
    alerting: Optional[bool] = None
    rules: Optional[List[BaselineRuleCreate]] = None  # replace-all when provided


class BaselineResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    enabled: bool
    os_type: Optional[str]
    site_id: Optional[str]
    site_name: Optional[str] = None
    match_tags: List[str] = Field(default_factory=list)
    alerting: bool
    rule_count: int = 0
    servers_evaluated: int = 0
    servers_compliant: int = 0
    violations: int = 0
    rules: List[BaselineRuleResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# ── Time-series response shape for charts ────────────────────────────

class MetricPoint(BaseModel):
    timestamp: datetime
    value: Optional[float] = None


class MetricSeries(BaseModel):
    metric: str
    unit: Optional[str] = None
    label: Optional[str] = None
    points: List[MetricPoint] = Field(default_factory=list)


class ServerMetricsResponse(BaseModel):
    server_id: str
    from_: datetime = Field(..., alias="from")
    to: datetime
    interval_s: int
    series: List[MetricSeries] = Field(default_factory=list)

    class Config:
        populate_by_name = True
