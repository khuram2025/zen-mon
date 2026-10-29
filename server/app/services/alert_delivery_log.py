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

Durable delivery keys (migrate-162, ``alert_delivery_keys``)
-------------------------------------------------------------

The log above is written *after* a send, so it can describe a duplicate but
never prevent one. A sender that knows which incident (``alert_id``) and which
lifecycle transition (``delivery_event``: "trigger", "recovery",
"escalation:L2:r1", "all_clear", ...) it is announcing claims one key per
(incident, event, channel, recipient) *before* it sends, and commits the claim:

    claimed    a worker owns this delivery (in flight); a claim older than
               10 minutes was interrupted before any outcome and may be
               claimed again
    accepted   the provider accepted it
    failed     the provider never took it (refused, rejected, misconfigured);
               the next attempt at the same event may claim it again
    uncertain  the transport failed in a way that does not tell whether the
               provider already accepted the message (a timeout or dropped
               connection after the data was sent); never re-sent, so a
               recipient may miss one rather than receive it twice

Concurrent workers, retries and restarts therefore never re-send a known
accepted (or possibly accepted) transition, while a genuinely new event — a new
incident, a recovery, the next escalation step — has a new key and is sent.
Exactly-once SMTP delivery is not promised; the outcome stays auditable here.
Migration 170 adds a fresh claim token for each attempt. A late completion from
an attempt taken over by another worker cannot overwrite the current receipt.
This protects database outcomes, not an external send already in progress.
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


class DeliveryNotSent(DeliveryRejected):
    """The transport failed before the message was handed over (connect,
    TLS or login): definitely not delivered, so a retry is safe."""


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


# ── Durable delivery keys ───────────────────────────────────────────────────

_keys_disabled = False

# Key states that mean "this recipient already has (or may have) the message".
DELIVERED_STATES = ("claimed", "accepted", "uncertain")


def outcome_for_exception(exc: BaseException | str | None) -> str:
    """"failed" when the error proves the provider never took the message,
    "uncertain" otherwise (it may have been accepted before the error)."""
    if exc is None or isinstance(exc, str):
        return "failed"
    import smtplib
    import socket
    import ssl
    definite: tuple = (
        DeliveryRejected, ValueError, ConnectionRefusedError, socket.gaierror,
        ssl.SSLCertVerificationError,
        smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused,
        smtplib.SMTPSenderRefused, smtplib.SMTPConnectError, smtplib.SMTPHeloError,
        smtplib.SMTPNotSupportedError, smtplib.SMTPDataError,
    )
    try:
        import httpx
        definite += (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol,
                     httpx.InvalidURL)
    except ImportError:  # pragma: no cover - httpx is a server dependency
        pass
    return "failed" if isinstance(exc, definite) else "uncertain"


def keys_enabled() -> bool:
    return not _keys_disabled


def _keys_missing(exc: BaseException) -> bool:
    global _keys_disabled
    if "alert_delivery_keys" in str(exc) and "does not exist" in str(exc):
        if not _keys_disabled:
            logger.warning("durable delivery keys disabled until migrate-162 runs")
        _keys_disabled = True
        return True
    return False


async def delivered_recipients(db: AsyncSession, alert_id, event: str, *, strict=False) -> dict[str, str]:
    """{recipient: state} already claimed for this incident event on any channel
    (claimed / accepted / uncertain). Webhook-style keys (no recipient) are
    per channel and are left out."""
    if _keys_disabled:
        if strict:
            raise RuntimeError("Delivery key storage unavailable")
        return {}
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "SELECT recipient, state FROM alert_delivery_keys"
                " WHERE alert_id = CAST(:aid AS uuid) AND event = :ev AND recipient <> ''"
                " AND (state IN ('accepted', 'uncertain')"
                "      OR (state = 'claimed' AND claimed_at >= now() - interval '10 minutes'))"
            ), {"aid": str(alert_id), "ev": event})).all()
    except Exception as exc:
        if strict:
            raise
        if not _keys_missing(exc):
            logger.warning("delivery keys: lookup failed: %s", exc)
        return {}
    return {str(r[0]): str(r[1]) for r in rows}


async def claim_key(db: AsyncSession, alert_id, event: str, channel_id, recipient: str = "", *, strict=False
                    ) -> tuple[str | None, str | None]:
    """Claim one delivery. Returns (attempt token, None) when this caller owns it,
    (None, existing state) when it is already claimed/accepted/uncertain, and
    (None, None) when keys are unavailable (no table, the alert row is gone),
    in which case the caller sends without a key as before.

    A ``failed`` key is claimed again: nothing reached the recipient.
    The caller commits before sending.
    """
    if _keys_disabled:
        if strict:
            raise RuntimeError("Delivery key storage unavailable")
        return None, None
    params = {"aid": str(alert_id), "ev": event[:64], "cid": str(channel_id),
              "rcpt": recipient or ""}
    try:
        async with db.begin_nested():
            row = (await db.execute(text(
                "INSERT INTO alert_delivery_keys (alert_id, event, channel_id, recipient)"
                " VALUES (CAST(:aid AS uuid), :ev, CAST(:cid AS uuid), :rcpt)"
                " ON CONFLICT (alert_id, event, channel_id, recipient) DO UPDATE"
                " SET state = 'claimed', claimed_at = now(), finished_at = NULL, error = NULL,"
                " claim_token = gen_random_uuid()"
                " WHERE alert_delivery_keys.state = 'failed'"
                " OR (alert_delivery_keys.state = 'claimed'"
                "     AND alert_delivery_keys.claimed_at < now() - interval '10 minutes')"
                " RETURNING claim_token"
            ), params)).first()
            if row:
                return str(row[0]), None
            state = (await db.execute(text(
                "SELECT state FROM alert_delivery_keys WHERE alert_id = CAST(:aid AS uuid)"
                " AND event = :ev AND channel_id = CAST(:cid AS uuid) AND recipient = :rcpt"
            ), params)).scalar()
            return None, (str(state) if state else "claimed")
    except Exception as exc:
        if strict:
            raise
        if not _keys_missing(exc):
            logger.warning("delivery keys: claim failed for alert %s (%s): %s",
                           alert_id, event, exc)
        return None, None


async def finish_keys(db: AsyncSession, claim_tokens: list[str], state: str,
                      error: BaseException | str | None = None, *, strict=False) -> None:
    """Complete only the current, still-claimed attempt; ignore stale tokens."""
    if state not in ("accepted", "failed", "uncertain"):
        raise ValueError("Invalid delivery completion state")
    if not claim_tokens or _keys_disabled:
        if strict:
            raise RuntimeError("Delivery key storage unavailable")
        return
    err = sanitize_error(error) if not isinstance(error, str) else error
    try:
        async with db.begin_nested():
            await db.execute(text(
                "UPDATE alert_delivery_keys SET state = :st, error = :err, finished_at = now()"
                " WHERE claim_token = ANY(CAST(:tokens AS uuid[])) AND state = 'claimed'"
            ), {"st": state, "err": err, "tokens": list(claim_tokens)})
    except Exception as exc:
        if strict:
            raise
        if not _keys_missing(exc):
            logger.warning("delivery keys: could not record outcome %s: %s", state, exc)


def _uuid_or_none(value) -> str | None:
    if value in (None, ""):
        return None
    s = str(value)
    return s if re.fullmatch(r"[0-9a-fA-F-]{36}", s) else None


def reset_for_tests() -> None:
    global _disabled, _keys_disabled
    _disabled = False
    _keys_disabled = False
