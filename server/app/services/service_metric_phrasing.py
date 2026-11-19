"""Sentences for certificate-expiry and response-time alerts.

Self-contained on purpose: the general wording lives in alert_phrasing, which
other work owns; these three metrics only need a handful of sentences, and
they say what happened, to which asset, by when, against which limit:

    Certificate for shop.example.com (Service check Shop HTTPS) expires in 12 days
    (on 2026-12-01; limit 30 days)
    Certificate /Common/shop.crt on Load balancer f5-01 (10.0.0.9) expired 2 days ago
    (on 2026-11-17; limit 7 days)
    Response time of Service check Shop HTTPS is 2.4 s (limit 1.5 s, 5-minute average)

and on recovery

    Certificate for shop.example.com (Service check Shop HTTPS) renewed: now expires
    in 364 days (on 2027-11-18)
    Response time of Service check Shop HTTPS is back to 0.8 s (limit 1.5 s, 5-minute average)
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Optional

DAYS_METRICS = frozenset({"service_tls_days", "f5_cert_days"})
RESPONSE_METRIC = "service_response_ms"

METRIC_NOUNS = {
    "service_tls_days": "Certificate expiry",
    "f5_cert_days": "F5 certificate expiry",
    "service_response_ms": "Response time",
}


def days_text(days) -> str:
    n = int(days)
    return f"{n} day" + ("" if abs(n) == 1 else "s")


def ms_text(ms) -> str:
    """850 ms, 2.4 s, 12 s: milliseconds below a second, seconds from there."""
    v = float(ms)
    if abs(v) < 1000:
        return f"{v:.0f} ms"
    s = v / 1000.0
    if abs(s) >= 10:
        return f"{s:.0f} s"
    return f"{s:.1f}".rstrip("0").rstrip(".") + " s"


def window_text(seconds) -> str:
    """"5-minute average", "90-second average"."""
    s = int(seconds or 0)
    if s >= 60 and s % 60 == 0:
        return f"{s // 60}-minute average"
    return f"{s}-second average"


def value_text(metric: str, value) -> str:
    if value is None:
        return "unknown"
    return ms_text(value) if metric == RESPONSE_METRIC else days_text(value)


def limit_text(metric: str, threshold) -> str:
    if threshold is None:
        return ""
    return value_text(metric, float(threshold))


def date_text(when: Optional[datetime]) -> str:
    if when is None:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d")


def certificate_name(subject: Optional[str], fallback: str = "") -> str:
    """The certificate's common name from an X.509 subject string, else ``fallback``.

    Accepts "CN=shop.example.com,O=Shop" and "/C=US/CN=shop.example.com".
    """
    s = (subject or "").strip()
    m = re.search(r"(?:^|[,/]\s*)CN\s*=\s*([^,/]+)", s, flags=re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return fallback or s


def service_asset(check_name: str) -> str:
    return f"Service check {check_name}".strip()


def expiry_phrase(days, expires_on: Optional[datetime]) -> tuple[str, str]:
    """("expires in 12 days" | "expires today" | "expired 2 days ago", "2026-12-01" or "")."""
    n = int(days)
    on = date_text(expires_on)
    if n > 0:
        head = f"expires in {days_text(n)}"
    elif n == 0:
        head = "expires today"
    else:
        head = f"expired {days_text(-n)} ago"
    return head, on


def _who(cert: str, asset: str, *, joiner: str) -> str:
    if joiner == "for":
        return f"Certificate for {cert} ({asset})" if cert else f"Certificate of {asset}"
    return f"Certificate {cert} on {asset}" if cert else f"Certificate on {asset}"


def certificate_sentence(*, cert: str, asset: str, days, expires_on: Optional[datetime],
                         threshold, recovery: bool = False, renewed: bool = False,
                         joiner: str = "for") -> str:
    """One sentence for a certificate alert (trigger or recovery)."""
    who = _who(cert, asset, joiner=joiner)
    head, on = expiry_phrase(days, expires_on)
    limit = limit_text("service_tls_days", threshold)
    if recovery:
        when = f" (on {on})" if on else ""
        if renewed:
            return f"{who} renewed: now {head}{when}"
        tail = f" (on {on}; limit {limit})" if on and limit else (f" (limit {limit})" if limit else when)
        return f"{who} is back within the limit: {head}{tail}"
    parts = [p for p in (f"on {on}" if on else "", f"limit {limit}" if limit else "") if p]
    return f"{who} {head}" + (f" ({'; '.join(parts)})" if parts else "")


def response_sentence(*, asset: str, value, threshold, window_s: int,
                      recovery: bool = False) -> str:
    verb = "is back to" if recovery else "is"
    parts = [p for p in (f"limit {ms_text(threshold)}" if threshold is not None else "",
                         window_text(window_s)) if p]
    return f"Response time of {asset} {verb} {ms_text(value)} ({', '.join(parts)})"


def days_until(expires_on: datetime, now: datetime) -> int:
    """Whole days left, rounded down (an expiry 11.6 days away is "11 days")."""
    if expires_on.tzinfo is None:
        expires_on = expires_on.replace(tzinfo=timezone.utc)
    return int(math.floor((expires_on - now).total_seconds() / 86400.0))
