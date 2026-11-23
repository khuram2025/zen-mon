"""Who decides the role of an account (migrate-195).

* ``local``     - a local account: the role is whatever an administrator set.
* ``directory`` - an LDAP / RADIUS / OIDC account that follows its provider's
                  role mapping: the mapped role is applied at every sign-in.
* ``manual``    - a directory account whose role an administrator set by hand.
                  Sign-in keeps that role.

The rule, in one place so sign-in, the user API and the page say the same:

    The directory decides WHETHER a directory account may sign in (it must
    authenticate and match a role mapping or the default role). The mapping
    decides WHICH role it holds, unless an administrator set one by hand; a
    hand-set role stays until an administrator hands the account back to the
    directory.

LDAP listed-accounts mode also admits users with a role assigned in ZenPlus
after they authenticate in LDAP. Any mapped role is recorded separately
(``users.directory_role``).
"""
from __future__ import annotations

from typing import Optional

LOCAL = "local"
DIRECTORY = "directory"
MANUAL = "manual"


def is_external(user) -> bool:
    return (getattr(user, "auth_source", None) or "local") != "local"


def role_source(user) -> str:
    if not is_external(user):
        return LOCAL
    return MANUAL if getattr(user, "role_manual", False) else DIRECTORY


def uses_listed_role(source: str, cfg: dict, role_manual: bool) -> bool:
    """An administrator-assigned LDAP role can admit a listed account."""
    return source == "ldap" and not cfg.get("auto_provision") and role_manual


def apply_directory_role(user, mapped_role: Optional[str]) -> Optional[str]:
    """Sign-in of an existing directory account: record what the mapping gives
    and apply it unless the role was set by hand. Returns the previous role
    when the account's role changed, else None."""
    user.directory_role = mapped_role
    if mapped_role is None or getattr(user, "role_manual", False):
        return None
    if user.role == mapped_role:
        return None
    previous = user.role
    user.role = mapped_role
    return previous
