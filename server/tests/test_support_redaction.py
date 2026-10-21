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


# ---------------------------------------------------------------------------
# N4 (VAPT 2026-10-21): secret shapes that previously passed through.
# Every positive sample's secret fragment must disappear and the marker format
# must stay ``[REDACTED:<kind>]``; every negative sample must survive intact so
# bundles keep their debugging value.
# ---------------------------------------------------------------------------

POSITIVE_CORPUS = [
    # (text, secret fragment that must vanish, expected kind)
    ("url=https://hooks.slack.com/services/T0001/B0002/XXXXSECRETXXXXXXXXXX",
     "XXXXSECRETXXXXXXXXXX", "webhook_url"),
    ("https://hooks.slack.com/workflows/T01/A02/123/SlackWorkflowSecret",
     "SlackWorkflowSecret", "webhook_url"),
    ("https://contoso.webhook.office.com/webhookb2/aaaa-bbbb@cccc/IncomingWebhook/TEAMSSECRET123/dddd",
     "TEAMSSECRET123", "webhook_url"),
    ("https://outlook.office.com/webhook/aaa@bbb/IncomingWebhook/LegacyTeamsSecret/ccc",
     "LegacyTeamsSecret", "webhook_url"),
    ("POST https://discord.com/api/webhooks/123456789/DiscordSeCrEt_abc-def failed",
     "DiscordSeCrEt_abc-def", "webhook_url"),
    ("https://chat.example.net/hooks/mattermostsecretabcdef123",
     "mattermostsecretabcdef123", "webhook_url"),
    ("https://events.pagerduty.com/integration/PDINTEGRATIONKEY01/enqueue",
     "PDINTEGRATIONKEY01", "webhook_url"),
    ("https://api.telegram.org/bot123456:ABCdefGhIJKlmNoPQRsTUVwxyZ/sendMessage",
     "ABCdefGhIJKlmNoPQRsTUVwxyZ", "telegram_url"),
    ("telegram token 1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0 rejected",
     "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0", "telegram_token"),
    ("routing_key=R0UT1NGKEY", "R0UT1NGKEY", "env_secret"),
    ('{"routing_key": "pd-routing-123"}', "pd-routing-123", "json_secret"),
    ("integration_key: pd-integ-456", "pd-integ-456", "yaml_secret"),
    ("auth_key=snmp-auth-789", "snmp-auth-789", "env_secret"),
    ("priv_key=snmp-priv-789", "snmp-priv-789", "env_secret"),
    ("auth_password=snmp-authpw", "snmp-authpw", "env_secret"),
    ("priv_password=snmp-privpw", "snmp-privpw", "env_secret"),
    ("credential=c-secret-1", "c-secret-1", "env_secret"),
    ("credentials: c-secret-2", "c-secret-2", "yaml_secret"),
    ('log {"credentials": {"user": "admin", "pass": "nested-secret"}} end',
     "nested-secret", "json_secret"),
    ("license_key=LIC-KEY-0001", "LIC-KEY-0001", "env_secret"),
    ("smtp_pass=smtp-pass-1", "smtp-pass-1", "env_secret"),
    ("SMTP_PASSWORD=smtp-pass-2", "smtp-pass-2", "env_secret"),
    ("conn smtp_pass=smtp-pass-3; host=mail", "smtp-pass-3", "inline_secret"),
    ("sent header Bearer abcdefTOKEN123 to host", "abcdefTOKEN123", "bearer_token"),
    ("curl -H 'X-Agent-Key: agentsecret1' https://appliance", "agentsecret1", "secret_header"),
    ("X-Sensor-Key: sensorsecret1", "sensorsecret1", "secret_header"),
    ('headers={"X-Sensor-Key": "sensorsecret2", "Accept": "json"}', "sensorsecret2", "secret_header"),
    ("{'X-Agent-Key': 'agentsecret3', 'X-Agent-Id': 'a-1'}", "agentsecret3", "secret_header"),
    ("x-hub-signature-256: sha256=deadbeefcafe", "deadbeefcafe", "secret_header"),
    ("aws key AKIAIOSFODNN7EXAMPLE in env", "AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
    ("token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789 used",
     "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789", "github_token"),
    ("github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
     "github_pat_11ABCDEFG0123456789_abcdefghijklmnop", "github_token"),
    ("agent enrolled with zpa_q8KXl3pVb0mRnTt2Yw9eZs4u then",
     "zpa_q8KXl3pVb0mRnTt2Yw9eZs4u", "zenplus_key"),
    ("mysql -uroot -pS3cretPw db", "S3cretPw", "mysql_password"),
    ("mysqldump --password=S3cretPw2 db", "S3cretPw2", "cli_secret"),
    ("mysql --password S3cret3 db", "S3cret3", "cli_secret"),
    ("zenplus-tool --db-password hunter2x", "hunter2x", "cli_secret"),
    ("postgres://user:pgpass@host/db", "pgpass", "url_password"),
    ("smtp://alice:wonder@mail:25", "wonder", "url_password"),
    ("postgres://user:p@ss@w0rd9@db:5432/zenplus", "w0rd9", "url_password"),
    ("https://chat.googleapis.com/v1/spaces/AAA/messages?key=GKEY123&token=GTOK456",
     "GTOK456", "query_secret"),
    ("https://x.logic.azure.com/workflows/1/triggers/manual/paths/invoke?api-version=1&sig=AZSIG789",
     "AZSIG789", "query_secret"),
]

NEGATIVE_CORPUS = [
    "credential_id=42",
    '{"credential_id": 42, "auth_key_id": 7}',
    "X-Agent-Id: 1f0c2a44-agent",
    "X-Sensor-Id: sensor-7",
    "X-Appliance-ID: 550e8400-e29b-41d4-a716-446655440000",
    "X-Total-Count: 5",
    "device 550e8400-e29b-41d4-a716-446655440000 ok",
    "sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "commit 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",
    "GET https://zentryc.com/api/v1/updates/check 200",
    "https://example.com/api/v1/devices/12/interfaces",
    "import { useTags } from '@/hooks/useTags'",
    "Bearer authentication failed",
    "missing Bearer token",
    "mysql -uroot -p mydb",
    "psql -p 5432 -h db",
    "psql --no-password -h db",
    "--password-file /etc/zenplus/pw",
    '{"pass": 3, "fail": 0}',
    "bypass=1",
    "license_status=valid",
    "AKIAIOSFODNN7EXAMPLEX",
    "zpa_AbCd1234 key prefix",
    "at 12:34:56 ok",
]


@pytest.mark.parametrize("text,secret,kind", POSITIVE_CORPUS)
def test_n4_positive_corpus_is_redacted(text, secret, kind):
    r = Redactor()
    out = r.apply(text)
    assert secret not in out, out
    assert f"[REDACTED:{kind}]" in out, out
    # Redaction remains idempotent and never re-counts its own markers.
    again = Redactor()
    assert again.apply(out) == out
    assert again.counts == {}


@pytest.mark.parametrize("text", NEGATIVE_CORPUS)
def test_n4_negative_corpus_is_untouched(text):
    r = Redactor()
    assert r.apply(text) == text
    assert r.counts == {}


def test_n4_webhook_redaction_keeps_provider_visible():
    out = Redactor().apply("https://hooks.slack.com/services/T1/B2/SECRET")
    assert out.startswith("https://hooks.slack.com/")
    out = Redactor().apply("https://api.telegram.org/bot123:ABCDEF/getMe")
    assert out == "https://api.telegram.org/bot[REDACTED:telegram_url]/getMe"


def test_n4_structured_keys_cover_new_names_without_identifiers():
    r = Redactor()
    out = r.apply_structure({
        "routingKey": "r", "integration_key": "i", "smtp_pass": "s",
        "credentials": {"user": "u", "password": "p"}, "X-Agent-Key": "k",
        "webhook_url": "https://example.test/notify/abc",
        "credential_id": 5, "X-Agent-Id": "agent-1", "pass": 2,
    })
    for key in ("routingKey", "integration_key", "smtp_pass", "credentials",
                "X-Agent-Key", "webhook_url"):
        assert out[key] == "[REDACTED:structured_secret]", key
    assert out["credential_id"] == 5
    assert out["X-Agent-Id"] == "agent-1"
    assert out["pass"] == 2
    pairs = dict(redact_kv_pairs([("SMTP_PASS", "x"), ("AUTH_KEY", "y"), ("HOSTNAME", "h")]))
    assert pairs == {"SMTP_PASS": "[REDACTED:kv_secret]", "AUTH_KEY": "[REDACTED:kv_secret]",
                     "HOSTNAME": "h"}


def test_n4_json_bytes_remain_valid_json():
    import json as _json
    r = Redactor()
    raw = _json.dumps({"channel": {"config": {
        "url": "https://hooks.slack.com/services/T1/B2/SECRETPART",
        "routing_key": "abc"}}}).encode()
    out = _json.loads(r.apply_bytes(raw))
    assert "SECRETPART" not in _json.dumps(out)
    assert out["channel"]["config"]["routing_key"] == "[REDACTED:structured_secret]"
