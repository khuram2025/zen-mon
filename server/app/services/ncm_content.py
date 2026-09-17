"""Configuration representations: recoverable encrypted raw, comparison, display.

Comparison deliberately preserves secret/key/certificate changes. Display
redaction must never be used to decide whether the configuration changed.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from app.core import crypto

MAX_CONFIG_BYTES = 16 * 1024 * 1024
_NOISE = re.compile(
    r"^(?:! Last configuration change[^\n]*|! NVRAM config last updated[^\n]*|"
    r"ntp clock-period \d+|Building configuration[^\n]*|Current configuration : \d+ bytes|"
    r"#conf_file_ver=[^\n]*)$", re.M)
_PEM = re.compile(r"-----BEGIN [^\n]+-----.*?(?:-----END [^\n]+-----|\Z)", re.S)
_CERT_CHAIN = re.compile(r'^crypto pki certificate chain[^\n]*(?:\n[ \t]+[^\n]*)*', re.M)
_XML_SECRET = re.compile(r'<(password|phash|private-key|key|community|secret|auth-password|priv-password)(?:\s[^>]*)?>.*?</\1>', re.I | re.S)
# Conservatively redact the whole command containing potentially sensitive data.
_SECRET_LINE = re.compile(
    r"^.*(?:\b(?:password|passwd|secret|community|private-key|pre-shared-key|"
    r"psksecret|api-key|auth-key|authentication-key|encrypted-password|"
    r"key-string|shared-secret|cipher|authentication-key|privacy-key)\b|\bENC\s+|\b(?:radius|tacacs).*\bkey\b).*$", re.I | re.M)


def normalize_config(content: str) -> str:
    return re.sub(r"\n{2,}", "\n", _NOISE.sub("", content.replace("\r\n", "\n"))).strip()


def redact_config(content: str) -> str:
    content = _CERT_CHAIN.sub('<certificate chain redacted>', content)
    content = _XML_SECRET.sub('<sensitive element redacted>', content)
    return _SECRET_LINE.sub("<sensitive command redacted>", _PEM.sub("<PEM block redacted>", content))


def sensitive_signature(content: str) -> str:
    pem = _PEM.findall(content) + _CERT_CHAIN.findall(content)
    pem += [m.group(0) for m in _XML_SECRET.finditer(content)]
    commands = _SECRET_LINE.findall(_PEM.sub("", content))
    return hashlib.sha256("\n".join(pem + commands).encode()).hexdigest()


def config_diff(before: str, after: str, *, raw: bool = False, normalized: bool = True) -> dict:
    a, b = (normalize_config(before), normalize_config(after)) if normalized else (before, after)
    actual = list(difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm=""))
    added = sum(line.startswith('+') and not line.startswith('+++') for line in actual)
    removed = sum(line.startswith('-') and not line.startswith('---') for line in actual)
    sensitive = sensitive_signature(a) != sensitive_signature(b)
    display_a, display_b = (a, b) if raw else (redact_config(a), redact_config(b))
    display = '\n'.join(difflib.unified_diff(display_a.splitlines(), display_b.splitlines(),
                                            fromfile='before', tofile='after', lineterm=''))
    if actual and not display:
        display = '@@ Sensitive configuration changed; content redacted @@'
    return {'diff': display, 'added': added, 'removed': removed, 'identical': a == b,
            'sensitive_changed': sensitive, 'redacted': not raw, 'normalized': normalized}


def checked_content(content: str) -> str:
    content = content.replace('\r\n', '\n')
    if not content.strip():
        raise ValueError('Configuration is empty')
    if len(content.encode('utf-8')) > MAX_CONFIG_BYTES:
        raise ValueError('Configuration exceeds the 16 MiB limit')
    if '\x00' in content:
        raise ValueError('Text configuration contains NUL bytes')
    return content


def encrypt_content(content: str) -> bytes:
    return crypto.encrypt(checked_content(content))


def read_content(row) -> str:
    encrypted = getattr(row, 'content_enc', None)
    if encrypted is not None:
        content = crypto.decrypt(encrypted)
        expected = getattr(row, 'content_hash', None)
        if expected and hashlib.sha256(content.encode()).hexdigest() != expected:
            raise ValueError('Configuration integrity verification failed')
        return content
    # Existing rows remain readable until the explicit, resumable encryption
    # migration has completed. No new code writes plaintext configuration.
    return row.content or ''
