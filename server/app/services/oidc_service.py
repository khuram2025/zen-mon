"""OpenID Connect single sign-on (authorization-code flow with PKCE).

Configuration lives in ``system_settings`` key ``auth.oidc``. The flow:

1. ``GET /auth/oidc/login`` - builds the authorization URL (random state, nonce
   and PKCE verifier kept in a signed HttpOnly cookie, never in the URL) and
   redirects the browser to the identity provider.
2. ``GET /auth/oidc/callback`` - checks state against the cookie, exchanges the
   code (with the PKCE verifier) at the token endpoint, and validates the ID
   token: signature against the provider's JWKS, issuer, audience, expiry and
   nonce. Only asymmetric algorithms are accepted.
3. The validated claims become a ZenPlus account: matched by the stable ``sub``
   (never by a claim that can change or be reassigned), role from the group
   mapping, session created exactly like a password sign-in.

Nothing here trusts the browser: the token endpoint is called server-side and
the browser only ever receives a one-time exchange code.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

logger = logging.getLogger(__name__)

OIDC_SETTINGS_KEY = "auth.oidc"
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256", "PS384", "PS512")
CLOCK_LEEWAY_SECONDS = 60
HTTP_TIMEOUT = 10.0

OIDC_DEFAULTS: dict = {
    "enabled": False,
    "display_name": "Single sign-on",
    "issuer": "",
    "client_id": "",
    "client_secret": "",
    "scopes": "openid profile email",
    "username_claim": "preferred_username",
    "email_claim": "email",
    "name_claim": "name",
    "groups_claim": "groups",
    "group_mappings": [],   # [{"group": "NetAdmins", "role": "admin"}]
    "default_role": "",     # empty = deny users that match no mapping
    "auto_provision": True,
    "ca_certificate_pem": "",
    "redirect_base_url": "",   # blank = derive from the request (X-Forwarded-*)
    "revision": 0,
    "updated_at": "",
    "updated_by": "",
}


def merge_oidc(stored: Optional[dict]) -> dict:
    cfg = dict(OIDC_DEFAULTS)
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if k in OIDC_DEFAULTS})
    return cfg


class OidcError(Exception):
    """Configuration, connectivity or validation failure (message is user-safe)."""


# Overridable for tests: returns an httpx.AsyncClient.
def _client(cfg: dict) -> httpx.AsyncClient:
    verify: Any = True
    pem = cfg.get("ca_certificate_pem")
    if pem:
        import ssl
        verify = ssl.create_default_context(cadata=pem)
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=verify, follow_redirects=False)


def _is_local(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1")


def check_issuer_url(issuer: str) -> str:
    parsed = urlparse(issuer.strip())
    if parsed.scheme != "https" and not (parsed.scheme == "http" and _is_local(parsed.hostname or "")):
        raise OidcError("The issuer must be an https:// URL")
    if not parsed.hostname:
        raise OidcError("The issuer URL is not valid")
    return issuer.strip().rstrip("/")


_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_cache: dict[str, tuple[float, dict]] = {}
_CACHE_SECONDS = 600


def clear_caches() -> None:
    _discovery_cache.clear()
    _jwks_cache.clear()


async def discover(cfg: dict) -> dict:
    issuer = check_issuer_url(cfg.get("issuer") or "")
    hit = _discovery_cache.get(issuer)
    if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    url = issuer + "/.well-known/openid-configuration"
    try:
        async with _client(cfg) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        doc = resp.json()
    except Exception as exc:
        raise OidcError(f"Cannot read the provider's discovery document ({url}): {exc}") from exc
    if str(doc.get("issuer", "")).rstrip("/") != issuer:
        raise OidcError("The discovery document's issuer does not match the configured issuer")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not doc.get(key):
            raise OidcError(f"The discovery document has no {key}")
        parsed = urlparse(doc[key])
        if parsed.scheme != "https" and not (parsed.scheme == "http" and _is_local(parsed.hostname or "")):
            raise OidcError(f"{key} must be https")
    _discovery_cache[issuer] = (time.monotonic(), doc)
    return doc


async def _jwks(cfg: dict, doc: dict, *, force: bool = False) -> dict:
    uri = doc["jwks_uri"]
    hit = _jwks_cache.get(uri)
    if hit and not force and time.monotonic() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    try:
        async with _client(cfg) as client:
            resp = await client.get(uri)
        resp.raise_for_status()
        keys = resp.json()
    except Exception as exc:
        raise OidcError(f"Cannot read the provider's signing keys: {exc}") from exc
    _jwks_cache[uri] = (time.monotonic(), keys)
    return keys


def new_login_state() -> dict:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24),
            "verifier": verifier, "challenge": challenge}


def redirect_uri(cfg: dict, request_base: str) -> str:
    base = (cfg.get("redirect_base_url") or request_base).rstrip("/")
    return f"{base}/api/v1/auth/oidc/callback"


async def authorization_url(cfg: dict, redirect: str, st: dict) -> str:
    doc = await discover(cfg)
    params = {
        "response_type": "code", "client_id": cfg["client_id"], "redirect_uri": redirect,
        "scope": cfg.get("scopes") or "openid profile email", "state": st["state"], "nonce": st["nonce"],
        "code_challenge": st["challenge"], "code_challenge_method": "S256",
    }
    return doc["authorization_endpoint"] + ("&" if "?" in doc["authorization_endpoint"] else "?") + urlencode(params)


async def exchange_code(cfg: dict, redirect: str, code: str, verifier: str) -> dict:
    doc = await discover(cfg)
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
            "client_id": cfg["client_id"], "code_verifier": verifier}
    auth = None
    methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if cfg.get("client_secret"):
        if "client_secret_post" in methods and "client_secret_basic" not in methods:
            data["client_secret"] = cfg["client_secret"]
        else:
            auth = (cfg["client_id"], cfg["client_secret"])
    try:
        async with _client(cfg) as client:
            resp = await client.post(doc["token_endpoint"], data=data, auth=auth,
                                     headers={"Accept": "application/json"})
        body = resp.json()
    except Exception as exc:
        raise OidcError(f"The token endpoint could not be reached: {exc}") from exc
    if resp.status_code != 200 or "id_token" not in body:
        raise OidcError(f"The provider refused the sign-in: {body.get('error_description') or body.get('error') or resp.status_code}")
    return body


def _public_key(jwk: dict):
    kty = jwk.get("kty")
    if kty == "RSA":
        return RSAAlgorithm.from_jwk(json.dumps(jwk))
    if kty == "EC":
        return ECAlgorithm.from_jwk(json.dumps(jwk))
    raise OidcError(f"Unsupported signing key type {kty}")


async def validate_id_token(cfg: dict, id_token: str, nonce: str) -> dict:
    """Verify signature, issuer, audience, expiry and nonce; return the claims."""
    doc = await discover(cfg)
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OidcError("The ID token is malformed") from exc
    alg = header.get("alg")
    if alg not in ALLOWED_ALGORITHMS:
        raise OidcError(f"The ID token uses an unacceptable signing algorithm ({alg})")
    supported = doc.get("id_token_signing_alg_values_supported")
    if supported and alg not in supported:
        raise OidcError("The ID token algorithm is not one the provider advertises")
    kid = header.get("kid")

    async def find_key(force: bool):
        keys = (await _jwks(cfg, doc, force=force)).get("keys", [])
        candidates = [k for k in keys if (kid is None or k.get("kid") == kid) and k.get("use", "sig") == "sig"]
        return candidates[0] if candidates else None

    jwk = await find_key(False) or await find_key(True)   # one refresh handles key rotation
    if jwk is None:
        raise OidcError("No signing key matches the ID token")
    try:
        claims = jwt.decode(
            id_token, key=_public_key(jwk), algorithms=[alg], audience=cfg["client_id"],
            issuer=doc["issuer"], leeway=CLOCK_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise OidcError(f"The ID token was rejected: {exc}") from exc
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OidcError("The ID token nonce does not match this sign-in")
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != cfg["client_id"]:
        raise OidcError("The ID token's authorized party is not this application")
    return claims


def claim_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.replace(";", ",").split(",") if v.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return [str(value)]


def identity(cfg: dict, claims: dict) -> dict:
    """The account attributes carried by validated claims."""
    username = (claims.get(cfg.get("username_claim") or "preferred_username")
                or claims.get("email") or claims.get("sub"))
    return {
        "sub": str(claims["sub"]),
        "username": str(username).strip()[:100],
        "email": (claims.get(cfg.get("email_claim") or "email") or None),
        "full_name": (claims.get(cfg.get("name_claim") or "name") or None),
        "groups": claim_list(claims.get(cfg.get("groups_claim") or "groups")),
    }


def explain_group_role(cfg: dict, groups: list[str]) -> tuple[Optional[str], Optional[dict]]:
    lowered = [g.lower() for g in groups]
    for i, m in enumerate(cfg.get("group_mappings") or []):
        wanted = str(m.get("group", "")).strip().lower()
        role = m.get("role")
        if wanted and role and wanted in lowered:
            return role, {"kind": "mapping", "index": i, "match": m.get("group")}
    if cfg.get("default_role"):
        return cfg["default_role"], {"kind": "default"}
    return None, None
