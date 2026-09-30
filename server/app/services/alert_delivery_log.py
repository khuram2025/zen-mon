"""Per-channel notification delivery receipts (Alerts review A24/F04).

A rule's ``notify_channels`` is *policy*; ``alerts.metadata.notified`` only
says a dispatch was attempted. This log records what actually happened for
every channel of every alert notification:

    accepted  the transport completed — SMTP accepted the message, or the
              HTTP gateway/webhook answered 2xx
    failed    the transport raised (timeout, refused, HTTP >= 400, SMTP
              rejection); ``error`` holds a sanitised category + message
    skipped   nothing was sent: channel disabled, no recipients, no gateway

Recording is best-effort and isolated in a savepoint: a missing table (code
deployed before migrate-135) or any insert error never breaks alerting.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("zenplus.alert_delivery")

_disabled = False

_URL_RE = re.compile(r"(https?://[^/\s'\"]+)[^\s'\"]*")
_SECRETISH_RE = re.compile(r"(?i)(password|token|secret|apikey|api_key|authorization)=\S+")


class DeliveryRejected(Exception):
    """The provider answered, but did not accept the notification."""


def ensure_accepted(response) -> None:
    """Raise when an HTTP gateway/webhook answered with an error status."""
    status = getattr(response, "status_code", 200)
    if status >= 400:
        raise DeliveryRejected(f"HTTP {status}")


def sanitize_error(exc: BaseException | str | None) -> str | None:
    """Short, secret-free error text: URLs keep only scheme+host."""
    if exc is None:
        return None
    msg = str(exc) or exc.__class__.__name__
    msg = _URL_RE.sub(r"\1/…", msg)
    msg = _SECRETISH_RE.sub(r"\1=***", msg)
    category = error_category(exc)
    msg = msg.strip().replace("\n", " ")
    if len(msg) > 240:
        msg = msg[:237] + "…"
    return f"{category}: {msg}" if category else msg


def error_category(exc: BaseException | str | None) -> str:
    if exc is None:
        return ""
    name = exc.__class__.__name__.lower() if isinstance(exc, BaseException) else ""
    s = str(exc).lower()
    if "timeout" in name or "timed out" in s or "timeout" in s:
        return "timeout"
    if isinstance(exc, DeliveryRejected):
        return "rejected"
    if "smtpauthentication" in name or "authentication" in s:
        return "auth"
    if "smtprecipients" in name or "recipient" in s:
        return "recipient"
    if "connect" in name or "connection" in s or "refused" in s or "unreachable" in s:
        return "connection"
    if "https" in s and "require" in s:
        return "config"
    return "error"


def kind_from_ctx(ctx: dict | None, default: str = "trigger") -> str:
    c = ctx or {}
    if c.get("delivery_kind"):
        return str(c["delivery_kind"])
    if c.get("escalation_level"):
        return "escalation"
    if c.get("is_recovery") or c.get("resolved"):
        return "recovery"
    return default


class Timer:
    def __init__(self) -> None:
        self.start = time.monotonic()

    @property
    def ms(self) -> int:
        return int((time.monotonic() - self.start) * 1000)


async def record(db: AsyncSession, *, alert_id: Any = None, rule_id: Any = None,
                 channel_id: Any = None, channel_name: str | None = None,
                 channel_type: str | None = None, kind: str = "trigger",
                 outcome: str, error: BaseException | str | None = None,
                 latency_ms: int | None = None) -> None:
    global _disabled
    if _disabled:
        return
    try:
        async with db.begin_nested():
            await db.execute(text(
                "INSERT INTO alert_deliveries (alert_id, rule_id, channel_id, channel_name,"
                " channel_type, kind, outcome, error, latency_ms)"
                " VALUES (CAST(:aid AS uuid), CAST(:rid AS uuid), CAST(:cid AS uuid), :cname,"
                " :ctype, :kind, :outcome, :error, :ms)"
            ), {
                "aid": _uuid_or_none(alert_id), "rid": _uuid_or_none(rule_id),
                "cid": _uuid_or_none(channel_id), "cname": channel_name,
                "ctype": channel_type, "kind": kind[:30], "outcome": outcome,
                "error": sanitize_error(error) if not isinstance(error, str) else error,
                "ms": latency_ms,
            })
    except Exception as exc:
        if "alert_deliveries" in str(exc) and "does not exist" in str(exc):
            _disabled = True
            logger.warning("delivery log disabled until migrate-135 runs")
        else:
            logger.debug("delivery log insert failed: %s", exc)


def _uuid_or_none(value) -> str | None:
    if value in (None, ""):
        return None
    s = str(value)
    return s if re.fullmatch(r"[0-9a-fA-F-]{36}", s) else None


def reset_for_tests() -> None:
    global _disabled
    _disabled = False
