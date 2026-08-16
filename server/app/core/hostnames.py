"""Validation for operator-supplied connection targets.

Any field that makes the appliance *authenticate outbound* with a stored
credential — a domain controller entry, a credential's test target — is a
credential-disclosure primitive if the caller can point it anywhere: the
NTLM exchange for an account the caller was never given can be captured by
whoever chooses the host. Restricting the value to a bare hostname or IP
removes the URL tricks (scheme, path, embedded credentials, alternate port)
and leaves the remaining trust decision to the role check on the route.
"""

from __future__ import annotations

import ipaddress
import re

# RFC 1123 hostname: labels of letters/digits/hyphens, not starting or ending
# with a hyphen, up to 253 characters overall, optional root dot.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)


def is_bare_host(value: str | None) -> bool:
    """True when `value` is a plain hostname or IP literal and nothing else."""
    h = (value or "").strip()
    if not h:
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    return bool(_HOSTNAME_RE.match(h))


def normalize_host(value: str | None) -> str:
    """Return the trimmed host, or raise ValueError if it is not a bare host."""
    h = (value or "").strip()
    if not is_bare_host(h):
        raise ValueError(
            "must be a bare hostname or IP address "
            "(no scheme, port, path or credentials)"
        )
    return h
