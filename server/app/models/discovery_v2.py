"""SQLAlchemy models for Discovery v2 (Profiles, Schedules, Runs, etc.).

These wrap the schema introduced by migrate-017-discovery-v2.sql.
The legacy ``discovery_jobs`` / ``discovery_results`` tables (migrate-005)
remain for backward compatibility — Discovery v2 lives alongside them.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    Time,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DiscoveryProfile(Base):
    __tablename__ = "discovery_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    scope_type: Mapped[str] = mapped_column(String(20), nullable=False, default="cidr")
    targets: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    exclusions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    collector_id: Mapped[str | None] = mapped_column(String(80), nullable=True)

    protocols: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["icmp"])
    custom_ports: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    snmp_credential_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    windows_credential_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    ssh_credential_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    detect_lldp: Mapped[bool] = mapped_column(Boolean, default=True)
    detect_mac: Mapped[bool] = mapped_column(Boolean, default=True)
    detect_vendor: Mapped[bool] = mapped_column(Boolean, default=True)

    max_concurrency: Mapped[int] = mapped_column(Integer, default=32)
    scan_timeout_ms: Mapped[int] = mapped_column(Integer, default=2000)
    retry_count: Mapped[int] = mapped_column(Integer, default=1)
    rate_limit_pps: Mapped[int] = mapped_column(Integer, default=200)
    max_duration_sec: Mapped[int] = mapped_column(Integer, default=1800)

    import_mode: Mapped[str] = mapped_column(String(20), default="review")
    default_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="SET NULL"), nullable=True
    )
    default_tags: Mapped[list] = mapped_column(JSONB, default=list)
    default_template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_profiles.id", ondelete="SET NULL"), nullable=True
    )
    default_location: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    enable_monitoring: Mapped[bool] = mapped_column(Boolean, default=True)
    keep_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_recipients: Mapped[list] = mapped_column(JSONB, default=list)

    last_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class DiscoverySchedule(Base):
    __tablename__ = "discovery_schedules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_profiles.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    schedule_type: Mapped[str] = mapped_column(String(20), nullable=False)
    frequency: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cron_expression: Mapped[str | None] = mapped_column(String(120), nullable=True)
    interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_of_day: Mapped[time | None] = mapped_column(Time, nullable=True)
    day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str] = mapped_column(String(60), default="UTC")
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    maintenance_window: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_profiles.id", ondelete="CASCADE"), nullable=False
    )
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_schedules.id", ondelete="SET NULL"), nullable=True
    )
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    phase: Mapped[str] = mapped_column(String(40), default="preparing")
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)

    total_targets: Mapped[int] = mapped_column(Integer, default=0)
    completed_targets: Mapped[int] = mapped_column(Integer, default=0)
    responding_targets: Mapped[int] = mapped_column(Integer, default=0)
    failed_targets: Mapped[int] = mapped_column(Integer, default=0)
    new_devices: Mapped[int] = mapped_column(Integer, default=0)
    existing_devices: Mapped[int] = mapped_column(Integer, default=0)
    changed_devices: Mapped[int] = mapped_column(Integer, default=0)
    unknown_devices: Mapped[int] = mapped_column(Integer, default=0)
    ignored_devices: Mapped[int] = mapped_column(Integer, default=0)
    credential_failures: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_candidates: Mapped[int] = mapped_column(Integer, default=0)
    ready_to_import: Mapped[int] = mapped_column(Integer, default=0)

    config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    activity_log: Mapped[list] = mapped_column(JSONB, default=list)
    error_details: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DiscoveryResultV2(Base):
    __tablename__ = "discovery_results_v2"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_runs.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_profiles.id", ondelete="CASCADE"), nullable=False
    )

    ip_address: Mapped[str] = mapped_column(INET, nullable=False)
    mac_address: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fqdn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sys_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sys_object_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String(255), nullable=True)

    vendor: Mapped[str | None] = mapped_column(String(150), nullable=True)
    device_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    model: Mapped[str | None] = mapped_column(String(150), nullable=True)
    os: Mapped[str | None] = mapped_column(String(150), nullable=True)
    os_version: Mapped[str | None] = mapped_column(String(60), nullable=True)

    protocols_detected: Mapped[list] = mapped_column(JSONB, default=list)
    open_ports: Mapped[list] = mapped_column(JSONB, default=list)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    credential_status: Mapped[str] = mapped_column(String(20), default="not_tested")
    credential_used: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    windows_credential_used: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="unknown")
    matched_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    matched_template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_profiles.id", ondelete="SET NULL"), nullable=True
    )
    suggested_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="SET NULL"), nullable=True
    )
    suggested_tags: Mapped[list] = mapped_column(JSONB, default=list)
    confidence_score: Mapped[int] = mapped_column(Integer, default=0)

    conflict_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    conflict_with_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )

    import_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    imported: Mapped[bool] = mapped_column(Boolean, default=False)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imported_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    # FK to servers(id) exists at the DB level (migrate-031); no ORM ForeignKey
    # because the servers table has no SQLAlchemy model (raw-SQL module).
    imported_server_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    ignored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DiscoveryRule(Base):
    __tablename__ = "discovery_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_profiles.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    match_mode: Mapped[str] = mapped_column(String(10), default="all")
    conditions: Mapped[list] = mapped_column(JSONB, default=list)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    action_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DiscoveryIgnoredDevice(Base):
    __tablename__ = "discovery_ignored_devices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True, unique=True)
    mac_address: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ignored_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    ignored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DiscoveryImportBatch(Base):
    __tablename__ = "discovery_import_batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_runs.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_profiles.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    successful_items: Mapped[int] = mapped_column(Integer, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, default=0)
    skipped_items: Mapped[int] = mapped_column(Integer, default=0)
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="SET NULL"), nullable=True
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_profiles.id", ondelete="SET NULL"), nullable=True
    )
    snmp_credential_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    enable_monitoring: Mapped[bool] = mapped_column(Boolean, default=True)
    started_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_details: Mapped[str | None] = mapped_column(Text, nullable=True)


class WindowsCredential(Base):
    """Stored Windows credentials used by WMI / WinRM discovery probes."""
    __tablename__ = "windows_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(150), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(150), nullable=True)
    password_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    auth_method: Mapped[str] = mapped_column(String(20), default="ntlm")
    transport: Mapped[str] = mapped_column(String(10), default="http")
    port: Mapped[int] = mapped_column(Integer, default=5985)
    ssl_verify: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class DiscoveryImportItem(Base):
    __tablename__ = "discovery_import_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_import_batches.id", ondelete="CASCADE"), nullable=False
    )
    result_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("discovery_results_v2.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    # DB-level FK to servers(id) via migrate-031; no ORM ForeignKey (see above).
    server_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    conflict_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
