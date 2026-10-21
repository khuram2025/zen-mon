"""Organisation report-delivery settings (``system_settings['reports']``).

Share-link lifetime, run and artifact retention, the attachment size limit and
the delivery retry count (docs/reports-delivery.md). Missing or out-of-range
values fall back to the code defaults, so an appliance that never saved the
settings behaves as documented.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SETTINGS_KEY = "reports"

# name -> (default, minimum, maximum)
LIMITS: dict[str, tuple[int, int, int]] = {
    "link_expiry_days": (30, 0, 365),        # 0 = links never expire
    "run_retention_days": (180, 7, 730),
    "artifact_retention_days": (30, 1, 365),
    "max_attachment_mb": (10, 1, 50),
    "retry_attempts": (3, 0, 5),
}
DEFAULTS: dict[str, int] = {k: v[0] for k, v in LIMITS.items()}

# Minutes after the first attempt at which a failed delivery is tried again.
RETRY_DELAYS_MIN = (5, 15, 45, 120, 240)


def normalize(raw: Any) -> dict[str, int]:
    """Every setting as an int inside its range; unknown keys dropped."""
    src = raw if isinstance(raw, dict) else {}
    out: dict[str, int] = {}
    for key, (default, lo, hi) in LIMITS.items():
        try:
            value = int(src.get(key, default))
        except (TypeError, ValueError):
            value = default
        out[key] = min(hi, max(lo, value))
    return out


def validation_errors(raw: dict) -> list[str]:
    """Messages for unknown keys and values outside their range."""
    errors: list[str] = []
    for key, value in raw.items():
        if key not in LIMITS:
            errors.append(f"Unknown setting '{key}'")
            continue
        _default, lo, hi = LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            errors.append(f"{key} must be a whole number from {lo} to {hi}")
    return errors


async def load(db: AsyncSession) -> dict[str, int]:
    try:
        row = (await db.execute(
            text("SELECT value FROM system_settings WHERE key = :k"), {"k": SETTINGS_KEY})).first()
    except Exception:
        return dict(DEFAULTS)
    value = row[0] if row else None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    return normalize(value)


async def save(db: AsyncSession, values: dict[str, int]) -> dict[str, int]:
    merged = normalize({**await load(db), **values})
    await db.execute(
        text("INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb)) "
             "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
        {"k": SETTINGS_KEY, "v": json.dumps(merged)})
    return merged


def retry_delay_minutes(attempt: int) -> int:
    """Delay before retry number ``attempt`` (1-based)."""
    return RETRY_DELAYS_MIN[min(max(attempt, 1), len(RETRY_DELAYS_MIN)) - 1]
