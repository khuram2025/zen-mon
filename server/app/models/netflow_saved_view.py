import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, JSON, Boolean, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class NetflowSavedView(Base):
    """A saved NetFlow query (migrate-052, governance columns migrate-138).

    ``visibility`` is 'private' (owner only) or 'shared' (every NetFlow
    viewer; editable by the owner or netflow.manage). ``kind`` names the page
    the query belongs to and ``query_version`` its schema: 1 = the legacy flat
    forensics blob, 2 = ``{v: 2, kind, filter, window, ...}`` (contract §8).
    """

    __tablename__ = "netflow_saved_views"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=True)
    query: Mapped[dict] = mapped_column(JSON, nullable=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(String(16), default="shared", nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="forensics", nullable=False)
    query_version: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
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
