"""RFC 6238 time-based one-time passwords (no third-party dependency).

SHA-1 / 6 digits / 30 s: the parameters every authenticator app supports.
Verification accepts one step either side for clock drift and refuses a
counter that was already used (replay protection) when ``last_counter`` is
supplied.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
DRIFT_STEPS = 1


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    padded = secret_b32 + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded, casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(code).zfill(DIGITS)


def counter_for(ts: float | None = None) -> int:
    return int((time.time() if ts is None else ts) // STEP_SECONDS)


def verify(secret_b32: str, code: str, *, last_counter: int | None = None,
           now: float | None = None) -> int | None:
    """Return the matched counter, or None when the code is wrong / replayed."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    current = counter_for(now)
    matched = None
    for delta in range(-DRIFT_STEPS, DRIFT_STEPS + 1):
        counter = current + delta
        if hmac.compare_digest(_hotp(secret_b32, counter), code):
            matched = counter
    if matched is None:
        return None
    if last_counter is not None and matched <= last_counter:
        return None
    return matched


def provisioning_uri(secret_b32: str, account: str, issuer: str = "ZenPlus") -> str:
    label = quote(f"{issuer}:{account}")
    return (f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}")


def new_recovery_codes(count: int = 10) -> list[str]:
    """Single-use codes, shown once. 10 hex chars each, grouped for reading."""
    out = []
    for _ in range(count):
        raw = secrets.token_hex(5)
        out.append(f"{raw[:5]}-{raw[5:]}")
    return out


def hash_recovery_code(code: str) -> str:
    normalized = "".join(ch for ch in code.lower() if ch.isalnum())
    return hashlib.sha256(normalized.encode()).hexdigest()
