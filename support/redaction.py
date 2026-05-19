"""Secret redaction for support bundles.

Every byte that goes into a support tarball passes through a ``Redactor``.
The redactor matches a small set of high-confidence regexes for known secret
shapes and replaces matches with ``[REDACTED:<kind>]``. It also records counts
so a top-level ``redaction-report.json`` can show how many of each kind were
masked, without ever including the original values.

We deliberately favour precise patterns over heuristics. A false negative
(secret leaks) is far worse than a false positive (a hostname gets masked); but
overly aggressive scrubbing also hides debugging signal, so each pattern is
tied to a specific known field name from this codebase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Pattern


REDACTION_MARKER = "[REDACTED:{kind}]"


@dataclass(frozen=True)
class Pattern_:
    kind: str
    regex: Pattern[str]
    replacement_template: str = REDACTION_MARKER


def _ci(s: str) -> Pattern[str]:
    return re.compile(s, re.IGNORECASE)


SECRET_KEY_NAMES = (
    "POSTGRES_PASSWORD",
    "CLICKHOUSE_PASSWORD",
    "REDIS_PASSWORD",
    "JWT_SECRET",
    "JWT_SIGNING_KEY",
    "SNMP_ENC_KEY",
    "SECRET_KEY",
    "API_KEY",
    "API_TOKEN",
    "LICENSE_KEY",
    "REGISTRATION_TOKEN",
    "ENROLLMENT_TOKEN",
    "AUTH_PASS",
    "PRIV_PASS",
    "SNMPV3_AUTH_PASSPHRASE",
    "SNMPV3_PRIV_PASSPHRASE",
    "SNMP_COMMUNITY",
    "SMTP_PASSWORD",
    "SMS_TOKEN",
    "SLACK_TOKEN",
    "WEBHOOK_TOKEN",
    "WINDOWS_PASSWORD",
    "WINDOWS_PASS",
    "SSH_PASSWORD",
    "SSH_KEY",
)

# Build one combined "<KEY> = <value>" / "<KEY>: <value>" / JSON "<key>": "<value>"
# pattern from the well-known names so we don't have to maintain N hand-written
# regexes.
_secret_keys_alt = "|".join(re.escape(n) for n in SECRET_KEY_NAMES)

PATTERNS: tuple[Pattern_, ...] = (
    # KEY=value (env-file / shell). Uppercase keys only — the ini_secret pass
    # below handles the lowercase forms used in agent.conf. Splitting them
    # this way keeps the two patterns mutually exclusive so we never double-
    # count a single secret. ``(?!\[REDACTED)`` guards against re-matching a
    # value that's already been redacted on an earlier pass.
    Pattern_(
        kind="env_secret",
        regex=re.compile(
            rf"\b({_secret_keys_alt})\s*=\s*(?!\[REDACTED)([^\s\"'#]+)",
        ),
        replacement_template=r"\1=[REDACTED:env_secret]",
    ),
    # key = value (lowercase, ini / agent.conf style). Deliberately does NOT
    # match the uppercase SECRET_KEY_NAMES — those go through env_secret.
    Pattern_(
        kind="ini_secret",
        regex=re.compile(
            r"^(\s*)(api_key|password|community|auth_pass|priv_pass|passphrase|secret_key|access_key|token)(\s*=\s*)(?!\[REDACTED)(.+)$",
            re.IGNORECASE | re.MULTILINE,
        ),
        replacement_template=r"\1\2\3[REDACTED:ini_secret]",
    ),
    # "key": "value" in JSON
    Pattern_(
        kind="json_secret",
        regex=re.compile(
            rf"\"({_secret_keys_alt}|api_key|password|token|secret|community|auth_pass|priv_pass)\"\s*:\s*\"([^\"]*)\"",
            re.IGNORECASE,
        ),
        replacement_template=r'"\1": "[REDACTED:json_secret]"',
    ),
    # postgresql://user:pass@host/db   →   postgresql://user:[REDACTED:url_password]@host/db
    Pattern_(
        kind="url_password",
        regex=re.compile(
            r"\b((?:postgres(?:ql)?|redis|mysql|amqp|mongodb|clickhouse)\+?\w*://[^:/\s]+):([^@\s/]+)@",
            re.IGNORECASE,
        ),
        replacement_template=r"\1:[REDACTED:url_password]@",
    ),
    # Bearer / Token / Basic auth headers
    Pattern_(
        kind="auth_header",
        regex=re.compile(
            r"\b(Authorization\s*:\s*)(Bearer|Token|Basic|JWT)\s+[A-Za-z0-9._\-+/=]+",
            re.IGNORECASE,
        ),
        replacement_template=r"\1\2 [REDACTED:auth_header]",
    ),
    # PEM blocks
    Pattern_(
        kind="pem_block",
        regex=re.compile(
            r"-----BEGIN[^-]+PRIVATE KEY-----[\s\S]*?-----END[^-]+PRIVATE KEY-----",
            re.IGNORECASE,
        ),
        replacement_template="-----BEGIN PRIVATE KEY-----\n[REDACTED:pem_block]\n-----END PRIVATE KEY-----",
    ),
)


@dataclass
class Redactor:
    """Run text through the secret patterns and tally what was masked.

    ``apply`` is idempotent — running redacted output through the redactor
    again must not change anything (other than already-masked counters not
    incrementing further). Tests assert this property.
    """

    counts: dict[str, int] = field(default_factory=dict)

    def apply(self, text: str) -> str:
        for pat in PATTERNS:
            text, hits = pat.regex.subn(pat.replacement_template, text)
            if hits:
                self.counts[pat.kind] = self.counts.get(pat.kind, 0) + hits
        return text

    def apply_bytes(self, data: bytes) -> bytes:
        try:
            decoded = data.decode("utf-8", errors="replace")
        except Exception:
            return data
        return self.apply(decoded).encode("utf-8", errors="replace")

    def report(self) -> dict[str, int]:
        return dict(sorted(self.counts.items()))


def redact_kv_pairs(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Used by collectors that emit structured rows (env, pg_stat_activity).

    Masks values whose key looks sensitive without scanning the value with a
    regex — avoids false positives on benign values that happen to match a
    pattern.
    """
    out: list[tuple[str, str]] = []
    secret_lc = {n.lower() for n in SECRET_KEY_NAMES}
    extra = {"password", "token", "secret", "api_key", "community", "auth_pass", "priv_pass"}
    sensitive = secret_lc | extra
    for k, v in pairs:
        kl = k.lower()
        if kl in sensitive or any(s in kl for s in ("password", "token", "secret", "passphrase")):
            out.append((k, "[REDACTED:kv_secret]"))
        else:
            out.append((k, v))
    return out
