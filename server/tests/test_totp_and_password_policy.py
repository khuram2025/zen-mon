from __future__ import annotations

import base64

from app.core import password_policy, totp

# RFC 6238 Appendix B secret ("12345678901234567890"), SHA-1
SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")


def test_totp_matches_rfc6238_vectors():
    assert totp._hotp(SECRET, 59 // 30) == "287082"          # 8-digit 94287082 truncated to 6
    assert totp._hotp(SECRET, 1111111109 // 30) == "081804"
    assert totp._hotp(SECRET, 2000000000 // 30) == "279037"


def test_totp_verify_window_replay_and_garbage():
    now = 1111111109
    code = totp._hotp(SECRET, now // 30)
    counter = totp.verify(SECRET, code, now=now)
    assert counter == now // 30
    assert totp.verify(SECRET, code, now=now + 30) == counter       # one step of drift is tolerated
    assert totp.verify(SECRET, code, now=now + 120) is None          # but not more
    assert totp.verify(SECRET, code, last_counter=counter, now=now) is None   # replay refused
    for bad in ("", "12345", "abcdef", "1234567"):
        assert totp.verify(SECRET, bad, now=now) is None
    assert totp.verify(SECRET, " " + code[:3] + " " + code[3:], now=now) == counter


def test_recovery_codes_hash_and_normalize():
    codes = totp.new_recovery_codes(10)
    assert len(set(codes)) == 10
    assert totp.hash_recovery_code(codes[0]) == totp.hash_recovery_code(codes[0].upper().replace("-", " "))
    assert "otpauth://totp/ZenPlus%3Aalice" in totp.provisioning_uri(SECRET, "alice")


def test_password_policy_rules():
    p = password_policy.problems
    assert p("short") and p("elevenchars")                 # 6-11 characters is rejected (RP-14)
    assert p("password1234") and p("Password1234")          # common
    assert p("zzzzzzzzzzzzzz")                              # repetitive
    assert p("johnsmith-2024-x", username="johnsmith")      # contains username
    assert p("jsmith-correct-horse", email="jsmith@example.com")
    assert p("a" * 129)
    assert p("Correct-Horse-Battery-Staple-9", username="admin") == []
    assert p("just a long passphrase with spaces") == []    # no composition rules
    assert password_policy.requirements()["min_length"] == 12
