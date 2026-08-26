"""Manifest for a support bundle.

The manifest.json at the top of every bundle records what the bundle is, when
it was made, and which options were chosen. ``schema_version`` lets analysis
tooling on the support side detect incompatible bundle layouts as the feature
evolves.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import BUNDLE_SCHEMA_VERSION, __version__

ISSUE_CATEGORIES = (
    "all",
    "update_migration",
    "device_management",
    "snmp_discovery",
    "windows_credentials",
    "alerts_notifications",
    "performance_storage",
    "ui_api_error",
    "server_monitoring",
    "apm",
    "agent_upgrade",
    "appliance_reachability",
    "other",
)

TIME_RANGES = ("1h", "6h", "24h", "7d")


@dataclasses.dataclass
class BundleRequest:
    """Parsed request file written by the API process before kicking the worker.

    The worker reads this from ``/opt/zenplus/support/requests/<uuid>.json``
    and treats it as untrusted-but-already-authenticated input — it must still
    validate every field before use.
    """

    job_id: str
    issue_category: str
    issue_summary: str
    time_range: str
    include_extended_logs: bool
    requested_by: str
    created_at: str

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.issue_category not in ISSUE_CATEGORIES:
            errors.append(f"issue_category must be one of {ISSUE_CATEGORIES}")
        if self.time_range not in TIME_RANGES:
            errors.append(f"time_range must be one of {TIME_RANGES}")
        if len(self.issue_summary) > 500:
            errors.append("issue_summary exceeds 500 chars")
        if len(self.requested_by) > 255:
            errors.append("requested_by exceeds 255 chars")
        return errors

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "BundleRequest":
        extended = data.get("include_extended_logs", False)
        if not isinstance(extended, bool):
            raise ValueError("include_extended_logs must be a boolean")
        return cls(
            job_id=str(data["job_id"]),
            issue_category=str(data.get("issue_category", "other")),
            issue_summary=str(data.get("issue_summary", "")),
            time_range=str(data.get("time_range", "24h")),
            include_extended_logs=extended,
            requested_by=str(data.get("requested_by", "")),
            created_at=str(data.get("created_at", "")),
        )


def build_manifest(
    request: BundleRequest,
    *,
    appliance_id: str,
    hostname: str,
    version: str,
    started_at: datetime,
    completed_at: datetime,
    section_summaries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return the manifest dict to serialize as manifest.json."""

    return {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "worker_version": __version__,
        "appliance_id": appliance_id,
        "hostname": hostname,
        "zenplus_version": version,
        "started_at": _iso(started_at),
        "completed_at": _iso(completed_at),
        "duration_seconds": max(0, int((completed_at - started_at).total_seconds())),
        "request": {
            "job_id": request.job_id,
            "issue_category": request.issue_category,
            "issue_summary": request.issue_summary,
            "time_range": request.time_range,
            "include_extended_logs": request.include_extended_logs,
            "requested_by": request.requested_by,
            "created_at": request.created_at,
        },
        "sections": section_summaries,
    }


def write_manifest(target: Path, manifest: dict[str, Any]) -> None:
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _iso(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")
