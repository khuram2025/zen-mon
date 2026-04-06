import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan: Mapped[str] = mapped_column(String(50), default="trial")  # trial, professional, enterprise
    status: Mapped[str] = mapped_column(String(20), default="active")  # active, expired, cancelled
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_devices: Mapped[int] = mapped_column(Integer, default=50)
    max_service_checks: Mapped[int] = mapped_column(Integer, default=20)
    max_users: Mapped[int] = mapped_column(Integer, default=5)
    license_key: Mapped[str] = mapped_column(String(255), nullable=True)
    activated_by: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
