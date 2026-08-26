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
import json
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
    "OTEL_EXPORTER_OTLP_HEADERS",
    "SENSOR_API_KEY",
    "AGENT_API_KEY",
)

# Build one combined "<KEY> = <value>" / "<KEY>: <value>" / JSON "<key>": "<value>"
# pattern from the well-known names so we don't have to maintain N hand-written
# regexes.
_secret_keys_alt = "|".join(re.escape(n) for n in SECRET_KEY_NAMES)
_generic_secret_key = (
    r"[A-Za-z0-9_.-]*(?:password|passwd|passphrase|secret|token|"
    r"api[_-]?key|access[_-]?key|private[_-]?key|community)[A-Za-z0-9_.-]*"
)
_secret_key = rf"(?:{_secret_keys_alt}|{_generic_secret_key})"

PATTERNS: tuple[Pattern_, ...] = (
    # URLs with userinfo. Handle these before whole-value config assignment so
    # DATABASE_URL retains the useful scheme/user/host while losing only its
    # password. The scheme is intentionally generic: support logs may contain
    # HTTP(S), MSSQL/ODBC, AMQP, Elasticsearch, or vendor-specific DSNs.
    Pattern_(
        kind="url_password",
        regex=re.compile(
            r"\b([a-z][a-z0-9+.-]*://[^:/@\s]+):"
            r"(?!\[REDACTED:url_password\])([^@\s/]+)@",
            re.IGNORECASE,
        ),
        replacement_template=r"\1:[REDACTED:url_password]@",
    ),
    # Some clients encode a bearer token as URI userinfo without a password
    # (``https://token@host``). Preserve only the origin delimiter and host.
    Pattern_(
        kind="url_userinfo",
        regex=re.compile(
            r"\b([a-z][a-z0-9+.-]*://)"
            r"(?!\[REDACTED:)([^:/@\s]+)@",
            re.IGNORECASE,
        ),
        replacement_template=r"\1[REDACTED:url_userinfo]@",
    ),
    # KEY=value (env, shell, INI). This is line anchored and replaces the
    # complete value, so quoted values, spaces, # characters, and previously
    # unknown vendor keys such as CUSTOM_VENDOR_SECRET are safe. A generic
    # sensitive-key suffix is intentional: false negatives here leak secrets.
    Pattern_(
        kind="env_secret",
        regex=re.compile(
            rf"^(\s*(?:export\s+)?{_secret_key}\s*=\s*)"
            rf"(?![\"']?\[REDACTED:)[^\r\n]*$",
            re.IGNORECASE | re.MULTILINE,
        ),
        replacement_template=r"\1[REDACTED:env_secret]",
    ),
    # JSON object values. Match both the curated names and unknown keys whose
    # name clearly indicates secret material.
    Pattern_(
        kind="json_secret",
        regex=re.compile(
            rf'"({_secret_key})"\s*:\s*'
            rf'(?![\"\']?\[REDACTED:)'
            rf'(?:(?:"(?:\\.|[^"\\])*")|(?:\'(?:\\.|[^\'\\])*\')|[^,}}\r\n]+)',
            re.IGNORECASE,
        ),
        replacement_template=r'"\1": "[REDACTED:json_secret]"',
    ),
    # YAML and similar ``key: value`` formats. JSON is handled above first so
    # this pattern remains line-oriented and does not consume object syntax.
    Pattern_(
        kind="yaml_secret",
        regex=re.compile(
            rf"^(\s*{_secret_key}\s*:\s*)"
            rf"(?![\"']?\[REDACTED:)[^\r\n]*$",
            re.IGNORECASE | re.MULTILINE,
        ),
        replacement_template=r"\1[REDACTED:yaml_secret]",
    ),
    # Command-line flags in process output and journal entries.
    Pattern_(
        kind="cli_secret",
        regex=re.compile(
            r"\b(--(?:password|passwd|passphrase|secret|token|api[-_]key|"
            r"access[-_]key|private[-_]key|community))(\s*=\s*|\s+)"
            r"(?![\"']?\[REDACTED:)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s;|]+)",
            re.IGNORECASE,
        ),
        replacement_template=r"\1\2[REDACTED:cli_secret]",
    ),
    # Password-like key/value pairs embedded in ODBC-style connection strings
    # or command output (``Server=x;Password=y;``).
    Pattern_(
        kind="inline_secret",
        regex=re.compile(
            rf"\b({_secret_key})\s*=\s*"
            rf"(?![\"']?\[REDACTED:)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^;\s,}}]+)",
            re.IGNORECASE,
        ),
        replacement_template=r"\1=[REDACTED:inline_secret]",
    ),
    # Authorization and secret-bearing HTTP/gRPC headers. Authorization may
    # be rendered with ':' or '=' (OTEL exporter header syntax).
    Pattern_(
        kind="auth_header",
        regex=re.compile(
            r"\b((?:Proxy-)?Authorization\s*[:=]\s*)"
            r"(Bearer|Token|Basic|JWT)\s+"
            r"(?!\[REDACTED:)[A-Za-z0-9._\-+/=:%]+",
            re.IGNORECASE,
        ),
        replacement_template=r"\1\2 [REDACTED:auth_header]",
    ),
    # Non-standard clients sometimes send an opaque Authorization value with
    # no scheme. Mask that too; it is still a bearer credential in practice.
    Pattern_(
        kind="opaque_auth_header",
        regex=re.compile(
            r"\b((?:Proxy-)?Authorization\s*[:=])"
            r"(?!(?:\s*(?:Bearer|Token|Basic|JWT)\s+\[REDACTED:|"
            r"\s*\[REDACTED:))\s*[^\r\n\"]+",
            re.IGNORECASE,
        ),
        replacement_template=r"\1 [REDACTED:opaque_auth_header]",
    ),
    Pattern_(
        kind="secret_header",
        regex=re.compile(
            r"\b((?:X-API-Key|X-Auth-Token|X-Amz-Security-Token|"
            r"Grpc-Metadata-Authorization)\s*:\s*)"
            r"(?!\[REDACTED:)[^\r\n\"]*",
            re.IGNORECASE,
        ),
        replacement_template=r"\1[REDACTED:secret_header]",
    ),
    # Cookies are bearer credentials in practice. Mask the whole header rather
    # than trying to maintain an incomplete list of session-cookie names.
    Pattern_(
        kind="cookie_header",
        regex=re.compile(
            r"\b((?:Cookie|Set-Cookie)\s*:\s*)"
            r"(?!\[REDACTED:)[^\r\n\"]*",
            re.IGNORECASE,
        ),
        replacement_template=r"\1[REDACTED:cookie_header]",
    ),
    # Common query-string credentials in URLs printed by HTTP clients.
    Pattern_(
        kind="query_secret",
        regex=re.compile(
            r"([?&](?:access[_-]?token|refresh[_-]?token|api[_-]?key|"
            r"auth[_-]?token|password|passwd|passphrase|client[_-]?secret|"
            r"signature)\s*=\s*)"
            r"(?!\[REDACTED:)[^&#\s\"']+",
            re.IGNORECASE,
        ),
        replacement_template=r"\1[REDACTED:query_secret]",
    ),
    # XML configuration frequently carries the same sensitive names as YAML
    # and JSON but would otherwise bypass line-oriented assignment patterns.
    Pattern_(
        kind="xml_secret",
        regex=re.compile(
            rf"<({_secret_key})\b[^>]*>\s*"
            rf"(?!\[REDACTED:)[\s\S]*?</\1\s*>",
            re.IGNORECASE,
        ),
        replacement_template=r"<\1>[REDACTED:xml_secret]</\1>",
    ),
    # curl and similar tools accept HTTP basic credentials via ``-u`` or
    # ``--user``. Retain the username but never the password.
    Pattern_(
        kind="cli_userinfo",
        regex=re.compile(
            r"(?<!\S)(-u|--user)(\s+|\s*=\s*)([A-Za-z0-9_.@\\-]+):"
            r"(?!\[REDACTED:)[^\s\"';|]+",
            re.IGNORECASE,
        ),
        replacement_template=r"\1\2\3:[REDACTED:cli_userinfo]",
    ),
    # A JWT is usable even when it appears outside an Authorization header.
    Pattern_(
        kind="jwt",
        regex=re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\."
            r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
        ),
        replacement_template="[REDACTED:jwt]",
    ),
    # Free-form issue summaries or exception text often use phrases such as
    # "password is ..." instead of config syntax.
    Pattern_(
        kind="prose_secret",
        regex=re.compile(
            r"\b(password|passwd|passphrase|api\s+key|access\s+token|secret)"
            r"(\s+(?:is|was)\s+|\s*:\s*)"
            r"(?!\[REDACTED:)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\"']+)",
            re.IGNORECASE,
        ),
        replacement_template=r"\1\2[REDACTED:prose_secret]",
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
        # Preserve valid structured diagnostics as valid JSON. Regexes that
        # operate on an already-escaped JSON string can otherwise consume an
        # escape before a quote and make the support artifact unparsable.
        try:
            structured = json.loads(decoded)
        except (json.JSONDecodeError, ValueError):
            structured = None
        if isinstance(structured, (dict, list)):
            redacted = self.apply_structure(structured)
            suffix = "\n" if decoded.endswith(("\n", "\r")) else ""
            return (
                json.dumps(redacted, indent=2, sort_keys=True, ensure_ascii=False, default=str)
                + suffix
            ).encode("utf-8", errors="replace")
        return self.apply(decoded).encode("utf-8", errors="replace")

    def apply_structure(self, value):
        """Recursively scrub string leaves while retaining the data format."""
        if isinstance(value, dict):
            out = {}
            for key, child in value.items():
                if _is_sensitive_key(str(key)) and not (
                    isinstance(child, str) and child.startswith("[REDACTED:")
                ):
                    out[key] = "[REDACTED:structured_secret]"
                    self.counts["structured_secret"] = (
                        self.counts.get("structured_secret", 0) + 1
                    )
                else:
                    out[key] = self.apply_structure(child)
            return out
        if isinstance(value, list):
            return [self.apply_structure(child) for child in value]
        if isinstance(value, tuple):
            return [self.apply_structure(child) for child in value]
        if isinstance(value, str):
            return self.apply(value)
        return value

    def report(self) -> dict[str, int]:
        return dict(sorted(self.counts.items()))


def redact_kv_pairs(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Used by collectors that emit structured rows (env, pg_stat_activity).

    Masks values whose key looks sensitive without scanning the value with a
    regex — avoids false positives on benign values that happen to match a
    pattern.
    """
    out: list[tuple[str, str]] = []
    for k, v in pairs:
        if _is_sensitive_key(k):
            out.append((k, "[REDACTED:kv_secret]"))
        else:
            out.append((k, v))
    return out


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    known = {name.lower() for name in SECRET_KEY_NAMES}
    if normalized in known:
        return True
    return any(
        token in normalized
        for token in (
            "password", "passwd", "passphrase", "secret", "token",
            "api_key", "apikey", "access_key", "private_key", "community",
        )
    )
