"""Human-facing notification times use the same regional settings as reports.

Storage, API timestamps, event identity and duration calculations stay in UTC.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from app.services.branding import Branding, load_branding


async def notification_branding(db) -> Branding:
    try:
        # A failed settings read must not abort the caller's delivery transaction.
        async with db.begin_nested():
            return await load_branding(db, with_logos=False)
    except Exception:
        logging.getLogger(__name__).warning('Regional settings unavailable; notification times explicitly use UTC')
        return Branding()


def as_datetime(value) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def timestamp_label(value, branding: Branding, *, seconds=False) -> str:
    dt = as_datetime(value)
    if dt is None:
        return str(value or '')
    return branding.fmt_datetime(dt, seconds=seconds, with_tz=True)


# Older templates and queued messages contain these explicit UTC labels.
# Convert their original instants, never substitute the delivery time.
_UTC_LABELS = (
    (r'\b\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\b', '%Y-%m-%d %H:%M:%S UTC'),
    (r'\b\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\b', '%Y-%m-%d %H:%M UTC'),
    (r'\b\d{2}:\d{2} UTC on \d{2} [A-Za-z]{3} \d{4}\b', '%H:%M UTC on %d %b %Y'),
    (r'\b\d{2} [A-Za-z]{3} \d{4} \d{2}:\d{2} UTC\b', '%d %b %Y %H:%M UTC'),
)


def localize_text(value: str, branding: Branding) -> str:
    """Localize explicit generated UTC labels in human-readable template text."""
    def replace(match, pattern):
        try:
            dt = datetime.strptime(match[0], pattern).replace(tzinfo=timezone.utc)
            return timestamp_label(dt, branding, seconds='%S' in pattern)
        except ValueError:
            return match[0]
    for regex, pattern in _UTC_LABELS:
        value = re.sub(regex, lambda m: replace(m, pattern), value)
    return value
