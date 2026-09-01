import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, Integer, Float, Text, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.core.database import Base


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    metric: Mapped[str] = mapped_column(String(50), nullable=False)
    operator: Mapped[str] = mapped_column(String(10), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    duration: Mapped[int] = mapped_column(Integer, default=0)

    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True)
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("device_groups.id"), nullable=True)
    service_check_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("service_checks.id", ondelete="CASCADE"), nullable=True)
    service_check_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("service_check_groups.id", ondelete="SET NULL"), nullable=True)

    severity: Mapped[str] = mapped_column(String(20), default="warning")
    notify_channels: Mapped[dict] = mapped_column(JSONB, default=list)
    cooldown: Mapped[int] = mapped_column(Integer, default=300)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rule_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("alert_rules.id"), nullable=True)
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True)
    # No ORM-level ForeignKey: the servers table/model isn't always registered in
    # this metadata at mapper-config time. The DB FK (migrate-035) enforces it.
    server_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=True)
    service_check_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("service_checks.id", ondelete="CASCADE"), nullable=True)
    sensor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sensors.id", ondelete="SET NULL"), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="active")
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    acknowledged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    extra_data: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    # Relationships
    rule: Mapped["AlertRule | None"] = relationship("AlertRule", lazy="selectin", foreign_keys=[rule_id])
    device: Mapped["Device"] = relationship("Device", lazy="selectin", foreign_keys=[device_id])
    service_check = relationship("ServiceCheck", lazy="selectin", foreign_keys=[service_check_id])
    sensor = relationship("Sensor", lazy="selectin", foreign_keys=[sensor_id])
