"""Single source of truth for local-account password rules.

Follows NIST SP 800-63B: a length floor, a generous ceiling, a blocklist of
well-known / contextual passwords, and no composition or rotation rules. The
API enforces it (users.py, auth.py) and exposes it (``GET /auth/password-policy``)
so every browser form shows and checks exactly the same requirements.
"""

from __future__ import annotations

import re

MIN_LENGTH = 12
MAX_LENGTH = 128

# Passwords that satisfy the length floor yet appear at the top of every
# breach corpus. Compared case-insensitively after stripping separators.
_COMMON = frozenset({
    "password1234", "passw0rd1234", "password12345", "123456789012", "1234567890123",
    "qwertyuiop12", "qwertyuiopas", "qwerty123456", "qwertyuiop123", "iloveyou1234",
    "administrator", "admin1234567", "admin123456789", "adminadmin12", "letmein12345",
    "welcome12345", "welcome12345!", "changeme1234", "changemenow1", "monkey123456",
    "abc123456789", "abcdefghijkl", "zxcvbnm12345", "1q2w3e4r5t6y", "1qaz2wsx3edc",
    "password!234", "p@ssw0rd1234", "p@ssword1234", "passw0rd!234", "trustno1trust",
    "zenplus12345", "zenplus123456", "zenplusadmin", "zentryc12345", "monitoring123",
    "networkadmin", "network12345", "cisco1234567", "ciscocisco123", "superuser1234",
    "000000000000", "111111111111", "aaaaaaaaaaaa", "123123123123", "121212121212",
})


def _norm(value: str) -> str:
    return re.sub(r"[\s_\-.]+", "", value).lower()


def requirements() -> dict:
    """Machine-readable policy for the browser forms."""
    return {
        "min_length": MIN_LENGTH,
        "max_length": MAX_LENGTH,
        "rules": [
            f"At least {MIN_LENGTH} characters (up to {MAX_LENGTH}); a passphrase works well",
            "Not a commonly used or previously breached password",
            "Must not contain your username or email name",
            "No special characters or periodic changes are required",
        ],
    }


def problems(password: str, *, username: str | None = None,
             email: str | None = None, full_name: str | None = None) -> list[str]:
    """Human-readable reasons ``password`` is not acceptable (empty = OK)."""
    out: list[str] = []
    if len(password) < MIN_LENGTH:
        out.append(f"Password must be at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        out.append(f"Password must be at most {MAX_LENGTH} characters")
    if out:
        return out
    flat = _norm(password)
    if flat in _COMMON or password.lower() in _COMMON:
        out.append("This password is too common; choose something less predictable")
    if len(set(password)) <= 3:
        out.append("Password is too repetitive")
    if re.fullmatch(r"(?:0123456789|1234567890|abcdefghij|qwertyuiop)+.*", flat) and len(set(flat)) <= 12:
        out.append("Password is a keyboard or number sequence")
    for label, token in (("username", username), ("email name", (email or "").split("@")[0])):
        token = _norm(token or "")
        if len(token) >= 4 and token in flat:
            out.append(f"Password must not contain your {label}")
    return out
