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

import asyncio
import bisect
import difflib
import hashlib
import re
import time
import weakref
from app.core import crypto

MAX_CONFIG_BYTES = 16 * 1024 * 1024
# Comparison bounds (see docs/ncm-change-detection.md, "Large comparisons").
# One comparison, both the counting and the display pass, must finish inside
# the time budget, and a display diff longer than the line cap is not sent to
# the browser. Either way the result says the diff is too large to show.
DIFF_TIME_BUDGET_SECONDS = 15.0
DIFF_MAX_DISPLAY_LINES = 20_000
DIFF_TOO_LARGE_MESSAGE = ('The differences are too large to show here. Download both versions '
                          'and compare them with a file comparison tool.')
# difflib.SequenceMatcher is quadratic when changes are interleaved (a
# FortiGate policy renumber: 120k lines took 36 s per pass), so it only sees
# inputs, or unmatched gaps, up to this many line pairs.
_DIFFLIB_MAX_PAIRS = 1_000_000
# Large comparisons running at once per API worker process.
_DIFF_CONCURRENCY = 2
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


class DiffTooLarge(Exception):
    """A comparison exceeded its time budget or display size."""


class _Deadline:
    def __init__(self, seconds: float):
        self.at = time.monotonic() + seconds

    def check(self) -> None:
        if time.monotonic() > self.at:
            raise DiffTooLarge('time budget exceeded')


def _longest_increasing(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Longest run of ``pairs`` (sorted by first index) increasing in the second."""
    tails: list[int] = []
    tail_values: list[int] = []
    previous: list[int | None] = [None] * len(pairs)
    for k, (_, j) in enumerate(pairs):
        at = bisect.bisect_left(tail_values, j)
        if at:
            previous[k] = tails[at - 1]
        if at == len(tails):
            tails.append(k)
            tail_values.append(j)
        else:
            tails[at] = k
            tail_values[at] = j
    out = []
    k = tails[-1] if tails else None
    while k is not None:
        out.append(pairs[k])
        k = previous[k]
    return out[::-1]


def _matching_blocks(a: list[str], b: list[str], deadline: _Deadline) -> list[tuple[int, int, int]]:
    """Matching line blocks in difflib's ``get_matching_blocks`` form.

    Small inputs use difflib unchanged. Large ones are anchored first on lines
    that occur exactly once on each side (patience diff); the gaps between
    anchors are matched the same way, and gaps small enough go to difflib. A
    large gap with no unique common line is reported as replaced, which is a
    valid if not minimal diff. Runs in roughly O(n log n) on configurations,
    where most lines (names, addresses, policy ids) are unique.
    """
    if len(a) * len(b) <= _DIFFLIB_MAX_PAIRS:
        return difflib.SequenceMatcher(None, a, b).get_matching_blocks()
    blocks: list[tuple[int, int, int]] = []
    ranges = [(0, len(a), 0, len(b))]
    while ranges:
        deadline.check()
        alo, ahi, blo, bhi = ranges.pop()
        same = 0
        while alo + same < ahi and blo + same < bhi and a[alo + same] == b[blo + same]:
            same += 1
        if same:
            blocks.append((alo, blo, same))
            alo, blo = alo + same, blo + same
        same = 0
        while alo < ahi - same and blo < bhi - same and a[ahi - 1 - same] == b[bhi - 1 - same]:
            same += 1
        if same:
            ahi, bhi = ahi - same, bhi - same
            blocks.append((ahi, bhi, same))
        if alo == ahi or blo == bhi:
            continue
        if (ahi - alo) * (bhi - blo) <= _DIFFLIB_MAX_PAIRS:
            matcher = difflib.SequenceMatcher(None, a[alo:ahi], b[blo:bhi])
            blocks.extend((alo + i, blo + j, n) for i, j, n in matcher.get_matching_blocks() if n)
            continue
        once_a: dict[str, int] = {}
        for i in range(alo, ahi):
            once_a[a[i]] = -1 if a[i] in once_a else i
        once_b: dict[str, int] = {}
        for j in range(blo, bhi):
            if once_a.get(b[j], -1) >= 0:
                once_b[b[j]] = -1 if b[j] in once_b else j
        anchors = _longest_increasing(sorted((once_a[line], j) for line, j in once_b.items() if j >= 0))
        pi, pj = alo, blo
        for i, j in anchors:
            blocks.append((i, j, 1))
            if i > pi and j > pj:
                ranges.append((pi, i, pj, j))
            pi, pj = i + 1, j + 1
        if anchors and pi < ahi and pj < bhi:
            ranges.append((pi, ahi, pj, bhi))
    merged: list[tuple[int, int, int]] = []
    for i, j, n in sorted(blocks):
        if merged and merged[-1][0] + merged[-1][2] == i and merged[-1][1] + merged[-1][2] == j:
            merged[-1] = (merged[-1][0], merged[-1][1], merged[-1][2] + n)
        else:
            merged.append((i, j, n))
    merged.append((len(a), len(b), 0))
    return merged


class _KnownBlocks(difflib.SequenceMatcher):
    """difflib's opcode grouping over precomputed matching blocks."""

    def __init__(self, a: list[str], b: list[str], blocks):
        super().__init__(None, (), ())
        self.a, self.b, self._known = a, b, blocks

    def get_matching_blocks(self):
        return self._known


def _unified_range(start: int, stop: int) -> str:
    length = stop - start
    if length == 1:
        return str(start + 1)
    return f'{start if not length else start + 1},{length}'


def _unified(a: list[str], b: list[str], blocks, max_lines: int) -> list[str]:
    """difflib.unified_diff(a, b, 'before', 'after', lineterm='') output."""
    out: list[str] = []
    for group in _KnownBlocks(a, b, blocks).get_grouped_opcodes(3):
        size = 1 + sum((i2 - i1 if tag != 'insert' else 0) + (j2 - j1 if tag in ('replace', 'insert') else 0)
                       for tag, i1, i2, j1, j2 in group)
        if len(out) + size + 2 > max_lines:
            raise DiffTooLarge('display too long')
        if not out:
            out += ['--- before', '+++ after']
        first, last = group[0], group[-1]
        out.append(f'@@ -{_unified_range(first[1], last[2])} +{_unified_range(first[3], last[4])} @@')
        for tag, i1, i2, j1, j2 in group:
            if tag == 'equal':
                out.extend(' ' + line for line in a[i1:i2])
                continue
            if tag in ('replace', 'delete'):
                out.extend('-' + line for line in a[i1:i2])
            if tag in ('replace', 'insert'):
                out.extend('+' + line for line in b[j1:j2])
    return out


def config_diff(before: str, after: str, *, raw: bool = False, normalized: bool = True, display: bool = True,
                budget: float = DIFF_TIME_BUDGET_SECONDS, max_display_lines: int = DIFF_MAX_DISPLAY_LINES) -> dict:
    """Compare two configurations. CPU-bound: request handlers must use
    ``config_diff_async``. A comparison over its time budget or display size
    returns ``too_large`` with a message instead of the diff (and without
    counts when even counting did not finish)."""
    deadline = _Deadline(budget)
    a, b = (normalize_config(before), normalize_config(after)) if normalized else (before, after)
    result = {'diff': '', 'added': 0, 'removed': 0, 'identical': a == b,
              'sensitive_changed': sensitive_signature(a) != sensitive_signature(b),
              'redacted': not raw, 'normalized': normalized, 'too_large': False}
    if result['identical']:
        return result
    a_lines, b_lines = a.splitlines(), b.splitlines()
    counted = False
    try:
        blocks = _matching_blocks(a_lines, b_lines, deadline)
        opcodes = _KnownBlocks(a_lines, b_lines, blocks).get_opcodes()
        result['added'] = sum(j2 - j1 for tag, _, _, j1, j2 in opcodes if tag in ('replace', 'insert'))
        result['removed'] = sum(i2 - i1 for tag, i1, i2, _, _ in opcodes if tag in ('replace', 'delete'))
        counted = True
        if display:
            if not raw:
                a_lines, b_lines = redact_config(a).splitlines(), redact_config(b).splitlines()
                blocks = _matching_blocks(a_lines, b_lines, deadline)
            result['diff'] = '\n'.join(_unified(a_lines, b_lines, blocks, max_display_lines))
            if not result['diff'] and any(op[0] != 'equal' for op in opcodes):
                result['diff'] = '@@ Sensitive configuration changed; content redacted @@'
    except DiffTooLarge:
        if not counted:
            result['added'] = result['removed'] = None
        result.update(diff='', too_large=True, message=DIFF_TOO_LARGE_MESSAGE)
    return result


_diff_slots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


async def config_diff_async(before: str, after: str, **options) -> dict:
    """``config_diff`` in a worker thread so the API event loop keeps serving
    other requests; at most ``_DIFF_CONCURRENCY`` run at once per process."""
    loop = asyncio.get_running_loop()
    slots = _diff_slots.get(loop)
    if slots is None:
        slots = _diff_slots[loop] = asyncio.Semaphore(_DIFF_CONCURRENCY)
    async with slots:
        return await asyncio.to_thread(config_diff, before, after, **options)


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
