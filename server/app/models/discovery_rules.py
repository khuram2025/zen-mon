"""Discovery rules (migrate-190): ordered global rules over scan facts.

See ``docs/discovery-rule-engine.md`` and ``app.services.discovery_rules``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DiscoveryEngineRule(Base):
    __tablename__ = "discovery_engine_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    asset_kinds: Mapped[list] = mapped_column(ARRAY(Text), nullable=False, default=lambda: ["device"])
    triggers: Mapped[list] = mapped_column(ARRAY(Text), nullable=False, default=lambda: ["discovery"])
    profile_ids: Mapped[list | None] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=True)
    condition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    actions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    enforcement: Mapped[str] = mapped_column(Text, nullable=False, default="set_once")
    on_unmatch: Mapped[str] = mapped_column(Text, nullable=False, default="keep")
    stop_processing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_changes_per_run: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    builtin_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    activated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_match_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DiscoveryEngineRuleVersion(Base):
    __tablename__ = "discovery_engine_rule_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_engine_rules.id", ondelete="CASCADE"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class DiscoveryEngineRun(Base):
    __tablename__ = "discovery_engine_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False, default="apply")
    discovery_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_runs.id", ondelete="SET NULL"), nullable=True)
    rules_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    counts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    undone_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # migrate-191
    targets: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    preview_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    undo_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_engine_runs.id", ondelete="SET NULL"), nullable=True)


class DiscoveryEngineRunItem(Base):
    __tablename__ = "discovery_engine_run_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_engine_runs.id", ondelete="CASCADE"), nullable=False)
    asset_kind: Mapped[str] = mapped_column(Text, nullable=False)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    discovery_result_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("discovery_results_v2.id", ondelete="SET NULL"), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    rule_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action_key: Mapped[str] = mapped_column(Text, nullable=False)
    item_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    before: Mapped[dict | list | str | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | list | str | None] = mapped_column(JSONB, nullable=True)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DiscoveryEngineAssignment(Base):
    """A value a rule set on a device or server (migrate-191)."""
    __tablename__ = "discovery_engine_assignments"

    asset_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    action_key: Mapped[str] = mapped_column(Text, primary_key=True)
    item_key: Mapped[str] = mapped_column(Text, primary_key=True, default="")
    value: Mapped[dict | list | str | int | bool | None] = mapped_column(JSONB, nullable=True)
    previous: Mapped[dict | list | str | int | bool | None] = mapped_column(JSONB, nullable=True)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    rule_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("discovery_engine_runs.id", ondelete="SET NULL"), nullable=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
