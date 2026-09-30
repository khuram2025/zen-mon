"""Global availability objective used when a service has no target of its own."""
from __future__ import annotations

import json
import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

SETTINGS_KEY = "availability.default_target_pct"
FALLBACK_TARGET_PCT = 99.9
MIN_TARGET_PCT = 50.0


def _coerce(value) -> float | None:
    if isinstance(value, dict):
        value = value.get("default_target_pct")
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if MIN_TARGET_PCT <= out <= 100 else None


async def load_default_target(db) -> tuple[float, str]:
    """(target, source): source is "policy" when an operator configured it."""
    try:
        row = (await db.execute(
            text("SELECT value FROM system_settings WHERE key = :key"), {"key": SETTINGS_KEY},
        )).first()
    except Exception:
        logger.warning("availability policy unavailable; using fallback target", exc_info=True)
        return FALLBACK_TARGET_PCT, "default"
    value = _coerce(row[0]) if row else None
    return (value, "policy") if value is not None else (FALLBACK_TARGET_PCT, "default")


async def save_default_target(db, value: float) -> None:
    await db.execute(
        text("INSERT INTO system_settings (key, value) VALUES (:key, CAST(:value AS jsonb)) "
             "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"),
        {"key": SETTINGS_KEY, "value": json.dumps(float(value))},
    )


def resolve_target(service_target, policy_target: float, policy_source: str) -> tuple[float, str]:
    """Per-service target, else the policy default (itself falling back to 99.9)."""
    if service_target is not None:
        try:
            return float(service_target), "service"
        except (TypeError, ValueError):
            pass
    return policy_target, policy_source
