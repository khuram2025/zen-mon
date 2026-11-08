"""Configuration representations: recoverable encrypted raw, comparison, display.

Comparison deliberately preserves secret/key/certificate changes. Display
redaction must never be used to decide whether the configuration changed.

One exception is forced by the devices themselves: values a device re-encrypts
with a fresh random salt every time it prints its configuration. FortiOS does
this for every reversible secret (``set psksecret ENC …``, ``set passwd ENC
…``, encrypted private keys), so two back-to-back captures of an unchanged
FortiGate differ on hundreds of lines. Those ciphertexts carry no comparable
information, so comparison replaces them with a fixed marker. Stored one-way
hashes that print identically every time (FortiOS administrator passwords,
``config system admin`` / ``set password ENC SH2…``) are still compared, and
the setting name stays in place, so adding or removing a secret is still a
change.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from app.core import crypto

MAX_CONFIG_BYTES = 16 * 1024 * 1024
# Bump when normalize_config changes meaning; stored norm_hash values are then
# recomputed by ncm_normalization.reconcile so baseline drift stays correct.
NORMALIZATION_VERSION = 2
ENCRYPTED_MARKER = '<re-encrypted by device>'
# FortiOS: ``set <name> ENC <ciphertext>`` (optionally quoted).
_FORTIOS_ENC = re.compile(r'^(\s*set\s+(\S+)\s+ENC\s+)("?)[^\s"]+\3(\s*)$')
_FORTIOS_CONFIG = re.compile(r'^\s*config\s+(.+?)\s*$')
_FORTIOS_END = re.compile(r'^\s*end\s*$')
# PEM private keys encrypted with a per-print salt/IV (PKCS#8 "ENCRYPTED
# PRIVATE KEY", or legacy "Proc-Type: 4,ENCRYPTED" with a DEK-Info IV).
_PEM_BLOCK = re.compile(r'-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----', re.S)
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


def _mask_encrypted_pem(match: re.Match) -> str:
    label, body = match.group(1), match.group(2)
    if 'PRIVATE KEY' in label and ('ENCRYPTED' in label or re.search(r'Proc-Type:\s*4,ENCRYPTED', body)):
        return f'-----BEGIN {label}-----{ENCRYPTED_MARKER}-----END {label}-----'
    return match.group(0)


def _mask_fortios_ciphertext(content: str) -> str:
    """Replace FortiOS per-print ciphertexts, keeping stable admin hashes."""
    if ' ENC ' not in content:
        return content
    path: list[str] = []
    out = []
    for line in content.split('\n'):
        opened = _FORTIOS_CONFIG.match(line)
        if opened:
            path.append(opened.group(1))
        elif _FORTIOS_END.match(line) and path:
            path.pop()
        else:
            enc = _FORTIOS_ENC.match(line)
            if enc and not (path and path[-1] == 'system admin' and enc.group(2) == 'password'):
                line = f'{enc.group(1)}{ENCRYPTED_MARKER}{enc.group(4)}'
        out.append(line)
    return '\n'.join(out)


def normalize_config(content: str) -> str:
    """Comparison form: device runtime noise and per-print ciphertext removed."""
    content = _NOISE.sub("", content.replace("\r\n", "\n"))
    content = _mask_fortios_ciphertext(_PEM_BLOCK.sub(_mask_encrypted_pem, content))
    return re.sub(r"\n{2,}", "\n", content).strip()


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


_NESTED_QUANTIFIER = re.compile(r'\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,)')
_BACKREFERENCE = re.compile(r'\\[1-9]')


def safe_pattern(pattern: str, *, max_length: int = 300):
    """Compile an operator-supplied regular expression, rejecting shapes that
    can backtrack catastrophically on large configurations (nested
    quantifiers, back-references). Raises ValueError with a safe message."""
    if not pattern or len(pattern) > max_length:
        raise ValueError(f'Pattern must be 1-{max_length} characters')
    if _NESTED_QUANTIFIER.search(pattern) or _BACKREFERENCE.search(pattern):
        raise ValueError('Pattern is too complex (nested quantifiers or back-references are not allowed)')
    try:
        return re.compile(pattern, re.I | re.M)
    except re.error as exc:
        raise ValueError(f'Invalid regular expression: {exc.msg}') from None


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
