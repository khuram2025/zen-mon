"""SNMP credential encryption.

Uses AES-256-GCM from the `cryptography` package (already a transitive
dependency via python-jose). The plan calls for libsodium secretbox, but
reusing `cryptography` avoids pulling in pynacl.

Ciphertext layout: 1-byte version || 12-byte nonce || ciphertext+tag.

The key is loaded from Settings.SNMP_ENC_KEY; accepted formats:
  - 64-char hex (32 bytes)
  - base64 / base64url encoding of 32 bytes
  - raw 32-byte UTF-8 string (strongly discouraged, but supported)
"""

from __future__ import annotations

import base64
import binascii
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

_VERSION = 0x01
_NONCE_LEN = 12
_KEY_LEN = 32


class CryptoError(Exception):
    """Raised when encryption/decryption fails or the key is not configured."""


def _decode_key(raw: str) -> bytes:
    if not raw:
        raise CryptoError(
            "SNMP_ENC_KEY is not set. Generate one with "
            "`python -c 'import secrets; print(secrets.token_hex(32))'` "
            "and add it to .env."
        )
    # hex
    if len(raw) == 64:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    # base64 / base64url
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            padded = raw + "=" * (-len(raw) % 4)
            candidate = decoder(padded)
            if len(candidate) == _KEY_LEN:
                return candidate
        except (binascii.Error, ValueError):
            continue
    # raw bytes fallback
    as_bytes = raw.encode("utf-8")
    if len(as_bytes) == _KEY_LEN:
        return as_bytes
    raise CryptoError(
        f"SNMP_ENC_KEY must decode to {_KEY_LEN} bytes (got {len(as_bytes)})."
    )


@lru_cache(maxsize=1)
def _cipher() -> AESGCM:
    return AESGCM(_decode_key(get_settings().SNMP_ENC_KEY))


def encrypt(plaintext: str | None) -> bytes | None:
    """Encrypt a string. Returns None if plaintext is None/empty."""
    if plaintext is None or plaintext == "":
        return None
    nonce = os.urandom(_NONCE_LEN)
    ct = _cipher().encrypt(nonce, plaintext.encode("utf-8"), None)
    return bytes([_VERSION]) + nonce + ct


def decrypt(token: bytes | memoryview | None) -> str | None:
    """Decrypt a token produced by encrypt(). Returns None for None input."""
    if token is None:
        return None
    buf = bytes(token)
    if len(buf) < 1 + _NONCE_LEN + 16:
        raise CryptoError("ciphertext too short")
    version = buf[0]
    if version != _VERSION:
        raise CryptoError(f"unsupported ciphertext version {version}")
    nonce = buf[1 : 1 + _NONCE_LEN]
    ct = buf[1 + _NONCE_LEN :]
    return _cipher().decrypt(nonce, ct, None).decode("utf-8")


def decrypt_secret(value: bytes | memoryview | str | None) -> str | None:
    """Return a SNMP secret whether it is encrypted (device columns) or plaintext
    (snmp_credentials rows)."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    buf = bytes(value)
    if not buf:
        return None
    if len(buf) >= 1 + _NONCE_LEN + 16 and buf[0] == _VERSION:
        try:
            return decrypt(buf)
        except CryptoError:
            pass
    return buf.decode("utf-8")


def is_configured() -> bool:
    """True if SNMP_ENC_KEY is set and decodes to a valid key."""
    try:
        _decode_key(get_settings().SNMP_ENC_KEY)
        return True
    except CryptoError:
        return False
