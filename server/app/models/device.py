import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, Integer, Float, Text, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, INET, JSONB

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
