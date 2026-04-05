import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, Integer, Float, Text, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.core.database import Base


class ServiceCheck(Base):
    __tablename__ = "service_checks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    check_type: Mapped[str] = mapped_column(String(20), nullable=False)
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
