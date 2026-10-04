"""SQLAlchemy models for the NetFlow governance tables (migrate-138).

The column shapes mirror scripts/migrate-138-netflow-governance.sql, which is
authoritative (CHECK constraints live there). Timestamps default in Python as
well as in the database so ORM-created rows carry them before a refresh.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, SmallInteger, String, Text, cast
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.core.database import Base


class InetType(TypeDecorator):
    """INET that binds and returns plain address strings.

    asyncpg sends a Python str bound to a plain INET column as VARCHAR, so an
    ORM filter such as ``Model.exporter_ip == "10.0.0.1"`` fails with
    "operator does not exist: inet = character varying". Casting every bind
    makes comparisons, inserts and updates work with plain address strings.
    Results come back as text too ("10.0.0.1", "10.0.0.5/24") rather than
    asyncpg's ``ipaddress`` objects, so rows match their ``Mapped[str]``
    annotations and serialize like the rest of the NetFlow API.
    """

    impl = postgresql.INET
    cache_ok = True

    def bind_expression(self, bindvalue):
        return cast(bindvalue, postgresql.INET)

    def process_result_value(self, value, dialect):
        return None if value is None else str(value)


class CidrType(InetType):
    """CIDR with the same string binds and results (see InetType)."""

    impl = postgresql.CIDR
    cache_ok = True

    def bind_expression(self, bindvalue):
        return cast(bindvalue, postgresql.CIDR)


INET, CIDR = InetType, CidrType


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _user_fk(**kw):
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, **kw
    )


class _Audited:
    """created/updated stamps shared by the operator-edited tables."""

    created_by: Mapped[uuid.UUID | None] = _user_fk()
    updated_by: Mapped[uuid.UUID | None] = _user_fk()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class NetflowIpGroup(_Audited, Base):
    """A named set of CIDRs (CR-13). ``parent_id`` nests groups: a parent's
    membership includes its descendants' CIDRs (services/netflow_groups.py)."""

    __tablename__ = "netflow_ip_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cidrs: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tags: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("netflow_ip_groups.id", ondelete="SET NULL"), nullable=True
    )


class NetflowAppRule(_Audited, Base):
    """An operator classification rule (CR-12): a port rule (``port_lo``
    [..``port_hi``], optional ``protocol`` and service-side ``cidrs``) or a
    protocol/address rule (no ports). Lower ``priority`` wins; built-ins sit
    at 1000 (services/netflow_classify.py)."""

    __tablename__ = "netflow_app_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application: Mapped[str] = mapped_column(String(80), nullable=False)
    service: Mapped[str | None] = mapped_column(String(80), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    protocol: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    port_lo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    port_hi: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cidrs: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class NetflowExporterSetting(_Audited, Base):
    """Per-exporter counting role and direction (FE-12/CR-16) and the export
    interval liveness is judged against (CR-05)."""

    __tablename__ = "netflow_exporter_settings"

    exporter_ip: Mapped[str] = mapped_column(INET, primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="edge", nullable=False)
    count_direction: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    expected_interval_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class NetflowInterfaceOverride(_Audited, Base):
    """Per exporter interface: display name, per-direction capacity speeds
    and role for NetFlow capacity (CR-08)."""

    __tablename__ = "netflow_interface_overrides"

    exporter_ip: Mapped[str] = mapped_column(INET, primary_key=True)
    ifindex: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    speed_in_bps: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    speed_out_bps: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class NetflowFinding(Base):
    """A persistent detection finding (CR-11) with its lifecycle."""

    __tablename__ = "netflow_findings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fingerprint: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    kind: Mapped[str | None] = mapped_column(String(48), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    src_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    dst_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    exporter_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurrences: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    alert_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    assigned_to: Mapped[uuid.UUID | None] = _user_fk()
    acknowledged_by: Mapped[uuid.UUID | None] = _user_fk()
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suppressed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suppressed_by: Mapped[uuid.UUID | None] = _user_fk()
    suppress_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class NetflowDetectorException(Base):
    """An approved exception: matching findings are kept and shown suppressed
    (optionally expiring, optionally limited to some exporters)."""

    __tablename__ = "netflow_detector_exceptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str | None] = mapped_column(String(48), nullable=True)
    src_cidr: Mapped[str | None] = mapped_column(CIDR, nullable=True)
    dst_cidr: Mapped[str | None] = mapped_column(CIDR, nullable=True)
    dst_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Exporters it applies to (migrate-140); None = every exporter. A
    # tag-scoped creator's exception is bound to the exporters it could see.
    exporter_ips: Mapped[list[str] | None] = mapped_column(ARRAY(postgresql.INET), nullable=True)
    created_by: Mapped[uuid.UUID | None] = _user_fk()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class NetflowTiFeed(_Audited, Base):
    """A threat-intelligence prefix list (CR-10)."""

    __tablename__ = "netflow_ti_feeds"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(48), default="malicious", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    refresh_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class NetflowTiEntry(Base):
    __tablename__ = "netflow_ti_entries"

    feed_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("netflow_ti_feeds.id", ondelete="CASCADE"), primary_key=True
    )
    prefix: Mapped[str] = mapped_column(CIDR, primary_key=True)


class NetflowExportJob(Base):
    """A complete raw-flow export produced in the background (netflow.export)."""

    __tablename__ = "netflow_export_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requested_by: Mapped[uuid.UUID | None] = _user_fk()
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    query: Mapped[dict] = mapped_column(JSONB, nullable=False)
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
