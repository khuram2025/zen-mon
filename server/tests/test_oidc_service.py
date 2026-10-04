"""OIDC ID-token validation and claim handling against an in-process mock IdP."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.services import oidc_service as o

ISSUER = "https://idp.example.test"
CLIENT = "zenplus-client"


def make_key(kid):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return key, jwk


class Idp:
    def __init__(self):
        self.key, self.jwk = make_key("k1")
        self.publish = [self.jwk]
        self.jwks_hits = 0
        self.last_token_request = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/jwks",
                "id_token_signing_alg_values_supported": ["RS256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic"]})
        if path == "/jwks":
            self.jwks_hits += 1
            return httpx.Response(200, json={"keys": self.publish})
        if path == "/token":
            self.last_token_request = request
            return httpx.Response(200, json={"id_token": self.token(nonce="n1")})
        return httpx.Response(404)

    def token(self, key=None, kid="k1", alg="RS256", **over):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": CLIENT, "sub": "user-1", "iat": now, "exp": now + 300, "nonce": "n1",
                  "preferred_username": "alice", "email": "alice@example.test", "groups": ["NetOps"], **over}
        return jwt.encode(claims, key or self.key, algorithm=alg, headers={"kid": kid})


@pytest.fixture
def idp(monkeypatch):
    idp = Idp()
    o.clear_caches()
    monkeypatch.setattr(o, "_client", lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(idp.handler)))
    return idp


CFG = {"issuer": ISSUER, "client_id": CLIENT, "client_secret": "s", "username_claim": "preferred_username",
       "groups_claim": "groups", "group_mappings": [{"group": "netops", "role": "operator"}], "default_role": ""}


@pytest.mark.asyncio
async def test_valid_token_yields_identity_and_role(idp):
    claims = await o.validate_id_token(CFG, idp.token(), "n1")
    ident = o.identity(CFG, claims)
    assert ident["sub"] == "user-1" and ident["username"] == "alice" and ident["groups"] == ["NetOps"]
    assert o.explain_group_role(CFG, ident["groups"])[0] == "operator"
    assert o.explain_group_role(CFG, ["other"]) == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("override,why", [
    ({"nonce": "other"}, "nonce"), ({"aud": "someone-else"}, "audience"), ({"iss": "https://evil"}, "issuer"),
    ({"exp": 1}, "expired"),
])
async def test_claims_are_checked(idp, override, why):
    with pytest.raises(o.OidcError):
        await o.validate_id_token(CFG, idp.token(**override), "n1")


@pytest.mark.asyncio
async def test_forged_and_downgraded_tokens_are_rejected(idp):
    other, _ = make_key("k1")
    with pytest.raises(o.OidcError):
        await o.validate_id_token(CFG, idp.token(key=other), "n1")            # wrong signature
    with pytest.raises(o.OidcError):
        await o.validate_id_token(CFG, jwt.encode({"sub": "x"}, key="", algorithm="none"), "n1")
    hs = jwt.encode({"iss": ISSUER, "aud": CLIENT, "sub": "x", "exp": 9999999999, "iat": 1, "nonce": "n1"},
                    key="shared-secret-confusion-attempt-0123456789", algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(o.OidcError, match="algorithm"):
        await o.validate_id_token(CFG, hs, "n1")                              # key-confusion downgrade


@pytest.mark.asyncio
async def test_key_rotation_refreshes_jwks_once(idp):
    await o.validate_id_token(CFG, idp.token(), "n1")
    new_key, new_jwk = make_key("k2")
    idp.publish = [new_jwk]
    hits = idp.jwks_hits
    claims = await o.validate_id_token(CFG, idp.token(key=new_key, kid="k2"), "n1")
    assert claims["sub"] == "user-1" and idp.jwks_hits == hits + 1


@pytest.mark.asyncio
async def test_issuer_must_be_https_and_match_discovery(idp):
    with pytest.raises(o.OidcError):
        o.check_issuer_url("http://idp.example.test")
    assert o.check_issuer_url("http://127.0.0.1:9000/") == "http://127.0.0.1:9000"
    o.clear_caches()
    with pytest.raises(o.OidcError, match="issuer"):
        await o.discover({**CFG, "issuer": ISSUER + "/tenant"})


@pytest.mark.asyncio
async def test_authorization_url_uses_pkce_and_exchange_sends_verifier(idp):
    st = o.new_login_state()
    url = await o.authorization_url(CFG, "https://zp.test/api/v1/auth/oidc/callback", st)
    q = parse_qs(urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"] and q["nonce"] == [st["nonce"]] and q["state"] == [st["state"]]
    expected = base64.urlsafe_b64encode(hashlib.sha256(st["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == [expected]
    await o.exchange_code(CFG, "https://zp.test/cb", "code123", st["verifier"])
    body = parse_qs(idp.last_token_request.content.decode())
    assert body["code_verifier"] == [st["verifier"]] and body["code"] == ["code123"]


def test_group_claim_shapes():
    assert o.claim_list("a, b;c") == ["a", "b", "c"]
    assert o.claim_list(None) == [] and o.claim_list(["x", 1]) == ["x", "1"]
    ident = o.identity({"username_claim": "nope"}, {"sub": "s1", "email": "e@x.test"})
    assert ident["username"] == "e@x.test"      # falls back to email, then sub
