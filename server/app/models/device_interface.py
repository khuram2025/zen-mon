import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Integer, BigInteger, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, MACADDR

from app.core.database import Base


class DeviceInterface(Base):
    __tablename__ = "device_interfaces"
    __table_args__ = (
        UniqueConstraint("device_id", "if_index", name="device_interfaces_device_ifindex_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    if_index: Mapped[int] = mapped_column(Integer, nullable=False)
    if_name: Mapped[str] = mapped_column(String(255), nullable=True)
    if_descr: Mapped[str] = mapped_column(String(255), nullable=True)
    if_alias: Mapped[str] = mapped_column(String(255), nullable=True)
    if_type: Mapped[int] = mapped_column(Integer, nullable=True)
    if_speed: Mapped[int] = mapped_column(BigInteger, nullable=True)
    mac_address: Mapped[str] = mapped_column(MACADDR, nullable=True)
    admin_status: Mapped[str] = mapped_column(String(20), nullable=True)
    oper_status: Mapped[str] = mapped_column(String(20), nullable=True)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
