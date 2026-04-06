"""Local update history tracking.

Stores update attempts in a JSON file so the dashboard can display
structured update history, errors, and progress.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("zenplus.updater")

HISTORY_FILE = Path("/opt/zenplus/updater/logs/update-history.json")
MAX_HISTORY = 50


def _load() -> list[dict]:
    try:
        if HISTORY_FILE.exists():
            return json.loads(HISTORY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _save(records: list[dict]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(records[-MAX_HISTORY:], indent=2))


def add_record(
    version: str,
    from_version: str,
    status: str,
    error: str = "",
    changelog: str = "",
    severity: str = "normal",
) -> dict:
    """Add or update a history record. Returns the record."""
    records = _load()

    # Find existing in-progress record for this version
    existing = None
    for r in records:
        if r.get("version") == version and r.get("status") in ("downloading", "applying"):
            existing = r
            break

    now = datetime.now(timezone.utc).isoformat()

    if existing:
        existing["status"] = status
        existing["updated_at"] = now
        if status in ("success", "failed", "rolled_back"):
            existing["completed_at"] = now
        if error:
            existing["error"] = error
        record = existing
    else:
        record = {
            "version": version,
            "from_version": from_version,
            "status": status,
            "changelog": changelog,
            "severity": severity,
            "error": error,
            "started_at": now,
            "updated_at": now,
            "completed_at": now if status in ("success", "failed", "rolled_back") else "",
        }
        records.append(record)

    _save(records)
    return record


def get_history() -> list[dict]:
    """Get all history records, newest first."""
    records = _load()
    records.reverse()
    return records


def get_latest() -> dict | None:
    """Get the most recent update record."""
    records = _load()
    return records[-1] if records else None


def get_active() -> dict | None:
    """Get the currently in-progress update, if any."""
    for r in reversed(_load()):
        if r.get("status") in ("downloading", "applying"):
            return r
    return None
