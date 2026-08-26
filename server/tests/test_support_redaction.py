from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from support.redaction import PATTERNS, Redactor, redact_kv_pairs  # noqa: E402


def test_env_password_is_masked():
    r = Redactor()
    out = r.apply("POSTGRES_PASSWORD=supersecret123\n")
    assert "supersecret123" not in out
    assert "[REDACTED:env_secret]" in out
    assert r.counts["env_secret"] == 1


def test_url_password_is_masked_but_user_and_host_preserved():
    r = Redactor()
    out = r.apply("DATABASE_URL=postgresql+asyncpg://zenplus:p%40ss@db.local:5432/zenplus")
    assert "p%40ss" not in out and "p@ss" not in out
    assert "zenplus:" in out  # username preserved up to colon
    assert "@db.local" in out  # host preserved
    assert "url_password" in r.counts


def test_token_only_url_userinfo_is_masked():
    out = Redactor().apply("GET https://secret-token@example.test/path")
    assert "secret-token" not in out
    assert "@example.test" in out


def test_embedded_cookies_and_opaque_authorization_are_masked():
    text = 'request headers="Cookie: session=topsecret" Authorization: opaque-secret\n'
    out = Redactor().apply(text)
    assert "topsecret" not in out
    assert "opaque-secret" not in out


def test_query_xml_and_curl_passwords_are_masked():
    text = (
        "https://example.test/?password=query-secret&safe=1\n"
        "<client_secret>xml-secret</client_secret>\n"
        "curl -u alice:cli-secret https://example.test\n"
    )
    out = Redactor().apply(text)
    for secret in ("query-secret", "xml-secret", "cli-secret"):
        assert secret not in out


def test_bearer_token_is_masked():
    r = Redactor()
    out = r.apply("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz")
    assert "eyJhbGc" not in out
    assert "[REDACTED:auth_header]" in out


def test_ini_password_in_agent_conf_is_masked():
    r = Redactor()
    conf = (
        "[appliance]\n"
        "id = 1234-abcd\n"
        "api_key = somesecretkey\n"
        "\n"
        "[server]\n"
        "url = https://zentryc.com\n"
    )
    out = r.apply(conf)
    assert "somesecretkey" not in out
    assert "1234-abcd" in out  # appliance id is *not* a secret
    assert "https://zentryc.com" in out


def test_json_secret_in_subscription_file_is_masked():
    r = Redactor()
    out = r.apply('{"api_key": "abc-def-ghi", "plan": "pro"}')
    assert "abc-def-ghi" not in out
    assert '"plan": "pro"' in out


def test_pem_private_key_is_replaced_wholesale():
    r = Redactor()
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIBVwIBADANBgkqhkiG9w0BAQEFAASCAUEwggE9AgEAAkEAlmS\n"
        "more-secret-bits\n"
        "-----END PRIVATE KEY-----\n"
    )
    out = r.apply(pem)
    assert "MIIBVwIBADANB" not in out
    assert "[REDACTED:pem_block]" in out


def test_redactor_is_idempotent():
    """Running the same text through the redactor twice must produce the same
    output the second time. If a replacement template ever matched its own
    output we'd silently inflate the counters or chain-replace."""
    r = Redactor()
    text = "POSTGRES_PASSWORD=abc\nAuthorization: Bearer xyz\n"
    once = r.apply(text)
    twice = Redactor().apply(once)
    assert once == twice


def test_report_only_returns_counts_not_values():
    r = Redactor()
    r.apply("POSTGRES_PASSWORD=very_secret\n")
    r.apply("api_key = another_secret\n")
    report = r.report()
    assert all(isinstance(v, int) for v in report.values())
    # The report must contain only counts — never anything that could be the
    # original secret value.
    for v in report.values():
        assert isinstance(v, int)


def test_kv_pairs_helper_masks_by_key_name():
    pairs = [("username", "alice"), ("password", "p4ss"), ("hostname", "edge-1")]
    out = dict(redact_kv_pairs(pairs))
    assert out["password"] == "[REDACTED:kv_secret]"
    assert out["username"] == "alice"
    assert out["hostname"] == "edge-1"


def test_every_pattern_kind_has_a_distinct_marker():
    """If two patterns share the exact same kind suffix our redaction-report
    counts collapse together. Catch any accidental collisions."""
    kinds = [p.kind for p in PATTERNS]
    assert len(kinds) == len(set(kinds))


def test_bytes_helper_round_trips_utf8():
    r = Redactor()
    out = r.apply_bytes(b"POSTGRES_PASSWORD=p\xc3\xa4ss\n")
    assert b"[REDACTED:env_secret]" in out
