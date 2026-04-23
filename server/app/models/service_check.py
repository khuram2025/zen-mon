import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, Integer, Float, SmallInteger, Text, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.core.database import Base


class ServiceCheckGroup(Base):
    __tablename__ = "service_check_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    color: Mapped[str] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ServiceCheck(Base):
    __tablename__ = "service_checks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=True)
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("service_check_groups.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    check_type: Mapped[str] = mapped_column(String(20), nullable=False)
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    target_host: Mapped[str] = mapped_column(String(255), nullable=False)
    target_port: Mapped[int] = mapped_column(Integer, nullable=True)
    target_url: Mapped[str] = mapped_column(String(2048), nullable=True)

    http_method: Mapped[str] = mapped_column(String(10), default="GET")
    http_headers: Mapped[dict] = mapped_column(JSONB, default=dict)
    http_body: Mapped[str] = mapped_column(Text, nullable=True)
    http_expected_status: Mapped[int] = mapped_column(Integer, default=200)
    http_content_match: Mapped[str] = mapped_column(String(1024), nullable=True)
    http_follow_redirects: Mapped[bool] = mapped_column(Boolean, default=True)

    tls_warn_days: Mapped[int] = mapped_column(Integer, default=30)
    tls_critical_days: Mapped[int] = mapped_column(Integer, default=7)

    check_interval: Mapped[int] = mapped_column(Integer, default=60)
    timeout: Mapped[int] = mapped_column(Integer, default=10)
    retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    retry_delay_s: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)
    parent_check_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("service_checks.id", ondelete="SET NULL"), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="unknown")
    last_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_response_ms: Mapped[float] = mapped_column(Float, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=True)

    tls_expiry_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    tls_days_remaining: Mapped[int] = mapped_column(Integer, nullable=True)
    tls_issuer: Mapped[str] = mapped_column(String(512), nullable=True)
    tls_subject: Mapped[str] = mapped_column(String(512), nullable=True)

    description: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    device = relationship("Device", lazy="selectin")
    group = relationship("ServiceCheckGroup", lazy="selectin")


class ServiceCheckTemplate(Base):
    __tablename__ = "service_check_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    check_type: Mapped[str] = mapped_column(String(20), nullable=False)
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    default_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    default_timeout: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    default_retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    default_retry_delay_s: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)
    target_url_template: Mapped[str] = mapped_column(String(2048), nullable=True)
    target_port_default: Mapped[int] = mapped_column(Integer, nullable=True)
    http_method: Mapped[str] = mapped_column(String(10), nullable=True)
    http_expected_status: Mapped[int] = mapped_column(Integer, nullable=True)
    http_content_match: Mapped[str] = mapped_column(String(1024), nullable=True)
    http_follow_redirects: Mapped[bool] = mapped_column(Boolean, nullable=True)
    tls_warn_days: Mapped[int] = mapped_column(Integer, nullable=True)
    tls_critical_days: Mapped[int] = mapped_column(Integer, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ServiceCheckMaintenance(Base):
    __tablename__ = "service_check_maintenance"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)  # check|group|tag|all
    scope_check_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("service_checks.id", ondelete="CASCADE"), nullable=True)
    scope_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("service_check_groups.id", ondelete="CASCADE"), nullable=True)
    scope_tag: Mapped[str] = mapped_column(String(120), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    check = relationship("ServiceCheck", foreign_keys=[scope_check_id], lazy="selectin")
    group = relationship("ServiceCheckGroup", foreign_keys=[scope_group_id], lazy="selectin")
