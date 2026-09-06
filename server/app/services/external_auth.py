"""External authentication: LDAP / Active Directory and RADIUS.

Configuration lives in the ``system_settings`` KV table under the keys
``auth.ldap`` and ``auth.radius`` (same pattern as ``security.tls``).
Both providers authenticate with the user's own credentials — LDAP via
service-account search + user bind, RADIUS via PAP Access-Request — and
map the result onto a ZenPlus role:

  LDAP    group_mappings: [{"group": <DN or CN>, "role": <role name>}]
          matched (first hit wins, case-insensitive) against the user's
          memberOf values; falls back to default_role.
  RADIUS  class_mappings: [{"value": <string>, "role": <role name>}]
          matched against the Class / Filter-Id reply attributes;
          falls back to default_role.

An empty default_role means "deny users that match no mapping".

ldap3 and pyrad are synchronous; callers run these via
``asyncio.to_thread``. Externally-authenticated users are provisioned as
rows in ``users`` with ``auth_source`` set and an unusable password hash,
so every other part of the app (audit, FKs, sessions) works unchanged.
"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Sentinel stored for external accounts; bcrypt will never verify it, so
# a local login against an external account always fails closed.
EXTERNAL_PASSWORD_HASH = "!external-auth!"

LDAP_SETTINGS_KEY = "auth.ldap"
RADIUS_SETTINGS_KEY = "auth.radius"

LDAP_DEFAULTS: dict = {
    "enabled": False,
    "server": "",
    "port": 389,
    "use_ssl": False,
    "use_starttls": True,
    "ca_certificate_pem": "",
    "bind_dn": "",
    "bind_password": "",
    "base_dn": "",
    "user_filter": "(sAMAccountName={username})",
    "email_attr": "mail",
    "name_attr": "displayName",
    "group_attr": "memberOf",
    "group_mappings": [],   # [{"group": "CN=NetAdmins,...", "role": "admin"}]
    "default_role": "",
    "auto_provision": True,
}

RADIUS_DEFAULTS: dict = {
    "enabled": False,
    "server": "",
    "port": 1812,
    "secret": "",
    "timeout": 5,
    "retries": 3,
    "nas_identifier": "zenplus",
    "class_mappings": [],   # [{"value": "netadmins", "role": "admin"}]
    "default_role": "viewer",
    "auto_provision": True,
}

_RADIUS_DICT_PATH = Path(__file__).with_name("radius_dictionary")


class ExternalAuthError(Exception):
    """Configuration or connectivity problem (not bad credentials)."""


def merge_config(defaults: dict, stored: Optional[dict]) -> dict:
    cfg = dict(defaults)
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if k in defaults})
    return cfg


# ── LDAP ──────────────────────────────────────────────────────────────────────

def _ldap_cn(dn: str) -> str:
    """First RDN value of a DN: 'CN=Net Admins,OU=...' -> 'net admins'."""
    head = dn.split(",", 1)[0]
    return (head.split("=", 1)[1] if "=" in head else head).strip().lower()


def map_ldap_role(cfg: dict, groups: list[str]) -> Optional[str]:
    lowered = [g.lower() for g in groups]
    cns = {_ldap_cn(g) for g in groups}
    for mapping in cfg.get("group_mappings") or []:
        wanted = str(mapping.get("group", "")).strip().lower()
        role = mapping.get("role")
        if not wanted or not role:
            continue
        if wanted in lowered or wanted in cns:
            return role
    return cfg.get("default_role") or None


def ldap_open(cfg: dict, user: Optional[str], password: Optional[str]):
    """Verify directory identity and finish TLS before any credential bind."""
    import ldap3
    if not cfg.get('use_ssl') and not cfg.get('use_starttls'):
        raise ExternalAuthError('LDAP requires LDAPS or StartTLS with certificate verification')
    tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_data=cfg.get('ca_certificate_pem') or None)
    server = ldap3.Server(cfg['server'], port=int(cfg.get('port') or (636 if cfg.get('use_ssl') else 389)),
                          use_ssl=bool(cfg.get('use_ssl')), tls=tls, connect_timeout=10)
    conn = ldap3.Connection(server, user=user or None, password=password or None,
                           receive_timeout=10, auto_bind=False, raise_exceptions=False, auto_referrals=False)
    try:
        conn.open()
        if conn.closed:
            raise ExternalAuthError('LDAP connection could not be established')
        if not cfg.get('use_ssl') and not conn.start_tls():
            raise ExternalAuthError('LDAP StartTLS failed; no credentials were sent')
        return conn
    except Exception:
        conn.unbind()
        raise


def ldap_test_bind(cfg: dict):
    conn = ldap_open(cfg, cfg.get('bind_dn'), cfg.get('bind_password'))
    try:
        if not conn.bind():
            raise ExternalAuthError('LDAP service bind failed')
    finally:
        conn.unbind()


def ldap_authenticate(cfg: dict, username: str, password: str) -> Optional[dict]:
    """Verify credentials against LDAP.

    Returns {"dn", "email", "full_name", "groups"} on success, None on
    bad credentials / unknown user. Raises ExternalAuthError when the
    directory itself is unreachable or misconfigured.
    """
    import ldap3
    from ldap3.core.exceptions import LDAPException
    from ldap3.utils.conv import escape_filter_chars

    if not password:
        return None
    if not cfg.get("server") or not cfg.get("base_dn"):
        raise ExternalAuthError("LDAP server and base DN are required")

    use_ssl = bool(cfg.get("use_ssl"))
    port = int(cfg.get("port") or (636 if use_ssl else 389))

    try:
        def _open(user, pwd):
            return ldap_open(cfg, user, pwd)

        search_conn = _open(cfg.get("bind_dn"), cfg.get("bind_password"))
        if not search_conn.bind():
            raise ExternalAuthError(
                f"LDAP service bind failed: {search_conn.result.get('description', 'invalid bind DN or password')}"
            )

        flt = (cfg.get("user_filter") or "(sAMAccountName={username})").replace(
            "{username}", escape_filter_chars(username)
        )
        attrs = [a for a in (cfg.get("email_attr"), cfg.get("name_attr"), cfg.get("group_attr")) if a]
        search_conn.search(cfg["base_dn"], flt, search_scope=ldap3.SUBTREE, attributes=attrs)
        if len(search_conn.entries) != 1:
            search_conn.unbind()
            return None

        entry = search_conn.entries[0]
        dn = entry.entry_dn

        def _attr_values(name: str) -> list[str]:
            try:
                return [str(v) for v in entry[name].values] if name in entry else []
            except (KeyError, LDAPException):
                return []

        email_vals = _attr_values(cfg.get("email_attr") or "mail")
        name_vals = _attr_values(cfg.get("name_attr") or "displayName")
        groups = _attr_values(cfg.get("group_attr") or "memberOf")
        search_conn.unbind()

        user_conn = _open(dn, password)
        ok = user_conn.bind()
        user_conn.unbind()
        if not ok:
            return None

        return {
            "dn": dn,
            "email": email_vals[0] if email_vals else None,
            "full_name": name_vals[0] if name_vals else None,
            "groups": groups,
        }
    except ExternalAuthError:
        raise
    except LDAPException as exc:
        raise ExternalAuthError(f"LDAP error: {exc}") from exc
    except OSError as exc:
        raise ExternalAuthError(f"Cannot reach LDAP server {cfg['server']}:{port}: {exc}") from exc


# ── RADIUS ────────────────────────────────────────────────────────────────────

def map_radius_role(cfg: dict, reply_values: list[str]) -> Optional[str]:
    lowered = [v.lower() for v in reply_values]
    for mapping in cfg.get("class_mappings") or []:
        wanted = str(mapping.get("value", "")).strip().lower()
        role = mapping.get("role")
        if not wanted or not role:
            continue
        if wanted in lowered:
            return role
    return cfg.get("default_role") or None


def radius_authenticate(cfg: dict, username: str, password: str) -> Optional[dict]:
    """Verify credentials via RADIUS PAP.

    Returns {"reply_values": [...]} on Access-Accept, None on
    Access-Reject. Raises ExternalAuthError on timeouts or bad config.
    """
    from pyrad import packet
    from pyrad.client import Client, Timeout
    from pyrad.dictionary import Dictionary

    if not password:
        return None
    if not cfg.get("server") or not cfg.get("secret"):
        raise ExternalAuthError("RADIUS server and shared secret are required")

    try:
        client = Client(
            server=cfg["server"],
            authport=int(cfg.get("port") or 1812),
            secret=str(cfg["secret"]).encode(),
            dict=Dictionary(str(_RADIUS_DICT_PATH)),
        )
        client.timeout = float(cfg.get("timeout") or 5)
        client.retries = int(cfg.get("retries") or 3)

        req = client.CreateAuthPacket(code=packet.AccessRequest, User_Name=username)
        req["User-Password"] = req.PwCrypt(password)
        if cfg.get("nas_identifier"):
            req["NAS-Identifier"] = str(cfg["nas_identifier"])

        req.add_message_authenticator()
        reply = client.SendPacket(req)
        try:
            verified = reply.verify_message_authenticator(secret=client.secret, original_authenticator=req.authenticator)
        except Exception:
            verified = False
        if not verified:
            raise ExternalAuthError("RADIUS response is missing a valid Message-Authenticator")
    except Timeout as exc:
        raise ExternalAuthError(
            f"RADIUS server {cfg['server']} did not respond (check server, port, and shared secret)"
        ) from exc
    except OSError as exc:
        raise ExternalAuthError(f"Cannot reach RADIUS server {cfg['server']}: {exc}") from exc

    if reply.code != packet.AccessAccept:
        return None

    values: list[str] = []
    for attr in ("Class", "Filter-Id", "Reply-Message"):
        try:
            for v in reply[attr]:
                values.append(v.decode() if isinstance(v, (bytes, bytearray)) else str(v))
        except KeyError:
            continue
    return {"reply_values": values}
