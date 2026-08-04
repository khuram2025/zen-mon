import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    String,
    Boolean,
    Integer,
    BigInteger,
    Float,
    Text,
    DateTime,
    ForeignKey,
    LargeBinary,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, INET, JSONB, MACADDR

from app.core.database import Base


class DeviceGroup(Base):
    __tablename__ = "device_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    color: Mapped[str] = mapped_column(String(7), nullable=True)
    parent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("device_groups.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    ip_address: Mapped[str] = mapped_column(INET, unique=True, nullable=False)
    device_type: Mapped[str] = mapped_column(String(50), default="other")
    location: Mapped[str] = mapped_column(String(255), nullable=True)
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("device_groups.id"), nullable=True)
    tags: Mapped[dict] = mapped_column(JSONB, default=list)

    # Monitoring config
    ping_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ping_interval: Mapped[int] = mapped_column(Integer, default=60)
    snmp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    snmp_community: Mapped[str] = mapped_column(String(255), nullable=True)
    snmp_version: Mapped[str] = mapped_column(String(5), default="2c")
    snmp_port: Mapped[int] = mapped_column(Integer, default=161)

    # SNMPv3 + advanced polling
    snmp_v3_username: Mapped[str] = mapped_column(String(255), nullable=True)
    snmp_v3_context: Mapped[str] = mapped_column(String(255), nullable=True)
    snmp_auth_protocol: Mapped[str] = mapped_column(String(16), nullable=True)
    snmp_auth_passphrase: Mapped[bytes] = mapped_column(LargeBinary, nullable=True)
    snmp_priv_protocol: Mapped[str] = mapped_column(String(16), nullable=True)
    snmp_priv_passphrase: Mapped[bytes] = mapped_column(LargeBinary, nullable=True)
    snmp_timeout_ms: Mapped[int] = mapped_column(Integer, default=2000)
    snmp_retries: Mapped[int] = mapped_column(Integer, default=2)
    snmp_max_repetitions: Mapped[int] = mapped_column(Integer, default=25)
    snmp_poll_interval: Mapped[int] = mapped_column(Integer, default=60)

    # Discovery results
    sys_object_id: Mapped[str] = mapped_column(String(255), nullable=True)
    vendor: Mapped[str] = mapped_column(String(100), nullable=True)
    model: Mapped[str] = mapped_column(String(255), nullable=True)
    os_version: Mapped[str] = mapped_column(String(255), nullable=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("device_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    snmp_credential_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )

    # Current state
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_rtt_ms: Mapped[float] = mapped_column(Float, nullable=True)

    # Metadata
    description: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    # Relationships
    group: Mapped["DeviceGroup"] = relationship("DeviceGroup", lazy="selectin")


class DeviceMaintenance(Base):
    """Planned-downtime window for devices (mirrors ServiceCheckMaintenance).

    While a window is active the poller suppresses status transitions and
    alerting for covered devices; SLA/uptime calculations exclude samples
    inside the window. Scope: one device, a group, a tag, or all devices.
    """

    __tablename__ = "device_maintenance"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)  # device|group|tag|all
    scope_device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=True)
    scope_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), nullable=True)
    scope_tag: Mapped[str] = mapped_column(String(120), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
