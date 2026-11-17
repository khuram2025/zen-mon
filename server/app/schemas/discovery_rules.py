"""Request/response models for discovery rules (``/api/v1/discovery-v2/rules``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

RuleMode = Literal["draft", "simulate", "active", "disabled"]
Trigger = Literal["discovery", "created"]
Enforcement = Literal["set_once", "enforce"]
AssetKind = Literal["device", "server"]


class RuleIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    description: Optional[str] = Field(default=None, max_length=1000)
    mode: RuleMode = "draft"
    asset_kinds: list[AssetKind] = Field(default_factory=lambda: ["device"], min_length=1)
    profile_ids: Optional[list[UUID]] = None
    condition: dict[str, Any] = Field(default_factory=lambda: {"op": "all", "conditions": []})
    actions: list[dict[str, Any]] = Field(default_factory=list)
    stop_processing: bool = False
    # When the rule runs by itself: on discovery scan results, and/or on
    # devices and servers when they are added. Manual runs on inventory can
    # use any active rule.
    triggers: list[Trigger] = Field(default_factory=lambda: ["discovery"], min_length=1)
    # set_once: a value edited by hand after the rule set it is left alone;
    # enforce: the rule puts its value back.
    enforcement: Enforcement = "set_once"


class RuleCreate(RuleIn):
    position: Literal["first", "last"] = "last"


class RuleUpdate(RuleIn):
    # Optimistic concurrency: the revision the editor loaded. A newer saved
    # revision answers 409 instead of silently overwriting someone's change.
    expected_revision: Optional[int] = None


class RuleModeRequest(BaseModel):
    mode: RuleMode


class RuleReorderRequest(BaseModel):
    ids: list[UUID] = Field(..., min_length=1)


class RuleOut(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    priority: int
    mode: str
    asset_kinds: list[str]
    triggers: list[str]
    profile_ids: Optional[list[UUID]] = None
    condition: dict[str, Any]
    actions: list[dict[str, Any]]
    stop_processing: bool
    enforcement: str = "set_once"
    revision: int
    created_by: Optional[UUID] = None
    updated_by: Optional[UUID] = None
    activated_by: Optional[UUID] = None
    activated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_evaluated_at: Optional[datetime] = None
    last_match_count: Optional[int] = None


class RuleVersionOut(BaseModel):
    revision: int
    snapshot: dict[str, Any]
    changed_by: Optional[UUID] = None
    changed_by_name: Optional[str] = None
    changed_at: datetime
    reason: Optional[str] = None


class EngineSettings(BaseModel):
    enabled: bool = False
    max_auto_import_per_run: int = Field(default=50, ge=1, le=1000)
    license_reserve_nodes: int = Field(default=0, ge=0, le=100000)
    history_days: int = Field(default=180, ge=7, le=3650)


class PreviewRule(RuleIn):
    """An unsaved rule as the editor holds it. ``id`` names the saved rule it
    replaces in the preview; ``priority`` places a new one (default: last)."""
    id: Optional[UUID] = None
    priority: Optional[int] = None
    name: str = Field(default="Unsaved rule", min_length=1, max_length=150)


class PreviewRequest(BaseModel):
    rule: Optional[PreviewRule] = None
    # with_active: the draft takes its place among the active rules (what a
    # scan would really do once it is active); only: the draft alone.
    scope: Literal["with_active", "only"] = "with_active"
    discovery_run_id: Optional[UUID] = None
    facts: Optional[dict[str, Any]] = None
    import_mode: Optional[Literal["review", "auto_match", "ignore_match"]] = None
    limit: int = Field(default=1000, ge=1, le=5000)


class EvaluateScanRequest(BaseModel):
    # False: store the plans on the results (Rule and Decision columns) and
    # act on nothing. True: also auto-import / ignore as the profile allows.
    apply: bool = False


class InventoryTargets(BaseModel):
    kinds: list[AssetKind] = Field(default_factory=lambda: ["device", "server"], min_length=1)
    device_ids: Optional[list[UUID]] = None
    server_ids: Optional[list[UUID]] = None
    group_ids: Optional[list[UUID]] = None
    tags: Optional[list[str]] = None
    subnet: Optional[str] = Field(default=None, max_length=64)
    query: Optional[str] = Field(default=None, max_length=200)


class InventoryRunRequest(BaseModel):
    # None: every active rule, in order. Otherwise only these active rules.
    rule_ids: Optional[list[UUID]] = None
    targets: InventoryTargets = Field(default_factory=InventoryTargets)


class InventoryApplyRequest(InventoryRunRequest):
    digest: str = Field(..., min_length=8, max_length=64)
