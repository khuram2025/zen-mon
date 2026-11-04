import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def now():
    return datetime.now(timezone.utc)


class VirtualizationConnector(Base):
    __tablename__ = "virtualization_connectors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(150))
    provider: Mapped[str] = mapped_column(String(30))
    endpoint: Mapped[str] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSONB)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_poll_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
