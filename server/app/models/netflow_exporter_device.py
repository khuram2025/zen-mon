import uuid
from datetime import datetime, timezone
from sqlalchemy import Text, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID, INET

from app.core.database import Base


class NetflowExporterDevice(Base):
    """Maps a NetFlow exporter source IP to the device that owns it.

    Routers frequently export flows from a loopback or WAN address rather than
    their SNMP management IP, which breaks the exporter_ip -> devices.ip_address
    match used to resolve ifIndex values into interface names. This mapping
    states that relationship explicitly. See migrate-054.
    """

    __tablename__ = "netflow_exporter_devices"

    exporter_ip: Mapped[str] = mapped_column(INET, primary_key=True)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
