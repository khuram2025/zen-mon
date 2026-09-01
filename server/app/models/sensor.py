"""ORM models for remote sensors / sites / assignments.

These mirror the migrate-008-sensors.sql schema. Most code paths in this
project use raw SQL via SQLAlchemy text() (see snmp_credentials.py for a
canonical example), so these models exist mainly for ORM-style reads
where convenient. The two API routers use raw SQL throughout, like the
rest of the code base.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    String,
    Integer,
    BigInteger,
    DateTime,
    ForeignKey,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, INET, JSONB

from app.core.database import Base


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    region: Mapped[str] = mapped_column(String(100), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    address: Mapped[str] = mapped_column(Text, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Sensor(Base):
    __tablename__ = "sensors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    site_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sites.id", ondelete="SET NULL"), nullable=True)
    location: Mapped[str] = mapped_column(String(255), nullable=True)

    enrollment_token_hash: Mapped[str] = mapped_column(String(128), nullable=True)
    enrollment_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    enrollment_consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    enrollment_consumed_ip: Mapped[str] = mapped_column(INET, nullable=True)
    api_key_hash: Mapped[str] = mapped_column(String(128), nullable=True)
    api_key_prefix: Mapped[str] = mapped_column(String(16), nullable=True)
    api_key_rotated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="pending")
    status_reason: Mapped[str] = mapped_column(Text, nullable=True)
    version: Mapped[str] = mapped_column(String(32), nullable=True)
    min_supported_version: Mapped[str] = mapped_column(String(32), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ip: Mapped[str] = mapped_column(INET, nullable=True)
    heartbeat_interval_s: Mapped[int] = mapped_column(Integer, default=30)
    degraded_after_s: Mapped[int] = mapped_column(Integer, default=90)
    offline_after_s: Mapped[int] = mapped_column(Integer, default=180)
    queue_depth: Mapped[int] = mapped_column(Integer, default=0)
    queue_dropped_count: Mapped[int] = mapped_column(BigInteger, default=0)

    hostname: Mapped[str] = mapped_column(String(255), nullable=True)
    os_info: Mapped[str] = mapped_column(String(255), nullable=True)
    uptime_seconds: Mapped[int] = mapped_column(BigInteger, nullable=True)

    bootstrap_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class SensorAssignment(Base):
    __tablename__ = "sensor_assignments"

    sensor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sensors.id", ondelete="CASCADE"),
        primary_key=True,
    )
    target_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
