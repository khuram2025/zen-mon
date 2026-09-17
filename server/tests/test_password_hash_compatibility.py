"""Compatibility vectors generated with the appliance's bcrypt 4.2.1 backend."""
import pytest

from app.core.security import hash_password, verify_password


@pytest.mark.parametrize("password,stored", [
    ("legacy-fixture", "$2b$04$abcdefghijklmnopqrstuu3x2ElAx6x4xb5eLFq7A2feTU9bSU0jS"),
    ("long-fixture-" * 10, "$bcrypt-sha256$2b,4$abcdefghijklmnopqrstuu$.JTljHMVIMauwlsAUL6DNoMWBQ9K7C."),
    ("long-fixture-" * 10, "$bcrypt-sha256$v=2,t=2b,r=4$abcdefghijklmnopqrstuu$cr9HWsYnNdh12giiw0Q1S1iBAy3InJm"),
])
def test_existing_hashes_survive_backend_upgrade(password, stored):
    assert verify_password(password, stored)
    assert not verify_password("incorrect-fixture", stored)


def test_long_unicode_password_round_trip_and_distinct_tail():
    prefix = "秘密" * 40
    stored = hash_password(prefix + "one")
    assert stored.startswith("$bcrypt-sha256$")
    assert verify_password(prefix + "one", stored)
    assert not verify_password(prefix + "two", stored)
