import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, BigInteger, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class DeviceEntity(Base):
    __tablename__ = "device_entities"
    __table_args__ = (
        UniqueConstraint("device_id", "ent_index", name="device_entities_device_entindex_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    ent_index: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_index: Mapped[int] = mapped_column(Integer, nullable=True)
    class_: Mapped[str] = mapped_column("class", String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=True)
    serial_number: Mapped[str] = mapped_column(String(255), nullable=True)
    model_name: Mapped[str] = mapped_column(String(255), nullable=True)
    hw_revision: Mapped[str] = mapped_column(String(64), nullable=True)
    fw_revision: Mapped[str] = mapped_column(String(64), nullable=True)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
