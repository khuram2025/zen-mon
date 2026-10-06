"""The seeded admin/admin123 login must not stay usable. These checks keep the
three places that know the seeded hash, and the paths that flag the account,
in agreement."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEED_HASH = re.compile(r"\$2b\$12\$vjHI8XBgL\.dCyn\.sgl41VufIFkQGcEzjt78GJdB66AwG9e9MZasai")
MIGRATION = ROOT / "scripts" / "migrate-153-force-default-admin-password-change.sql"


def read(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_the_seed_hash_is_the_same_everywhere_it_appears():
    """init-postgres.sql seeds it, install.sh recognises it, the migration targets it."""
    for relative in ("scripts/init-postgres.sql", "install.sh", MIGRATION.relative_to(ROOT).as_posix()):
        assert SEED_HASH.search(read(relative)), f"{relative} does not carry the seeded admin hash"


def test_the_seeded_hash_really_is_admin123():
    from passlib.context import CryptContext
    seed = SEED_HASH.search(read("scripts/init-postgres.sql")).group(0)
    assert CryptContext(schemes=["bcrypt"]).verify("admin123", seed)


def test_the_migration_only_ever_touches_the_admin_account_on_the_seeded_hash():
    sql = re.sub(r"--.*", "", MIGRATION.read_text())
    assert re.findall(r"\bUPDATE\b", sql, re.I) == ["UPDATE"]
    assert "username = 'admin'" in sql
    assert SEED_HASH.search(sql)
    assert "must_change_password = TRUE" in sql
    assert not re.search(r"\b(DELETE|DROP|TRUNCATE|ALTER)\b", sql, re.I)


def test_the_migration_is_recorded_in_the_append_only_lock():
    lock = read("scripts/migrations.lock")
    digest = hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
    assert f"{digest}  {MIGRATION.name}" in lock


def test_first_boot_requires_a_change_of_its_random_password():
    script = read("bin/first-boot-init.sh")
    update = re.search(r"UPDATE users\n(.*?)WHERE username = 'admin';", script, re.S).group(1)
    assert "password_hash = :'admin_hash'" in update
    assert "must_change_password = TRUE" in update


def test_the_installer_flags_the_seeded_admin_but_only_when_it_is_still_the_default():
    script = read("install.sh")
    block = re.search(
        r'if \[\[ "\$\{ADMIN_PASSWORD_IS_DEFAULT:-no\}" == "yes" \]\]; then(.*?)\n    fi\n', script, re.S)
    assert block, "install.sh must flag the admin only inside the default-password branch"
    assert "must_change_password = TRUE WHERE username = 'admin'" in block.group(1)
    # and the default flag is computed from the seeded hash, not assumed
    assert re.search(r'ADMIN_PASSWORD_IS_DEFAULT=\$\( \[\[ "\$stored" == \'\$2b\$12\$vjHI8', script)


def test_the_api_refuses_everything_but_a_password_change_to_a_flagged_account():
    security = read("server/app/core/security.py")
    gate = re.search(r"async def _enforce_sign_in_obligations(.*?)\n\n\nasync def", security, re.S).group(1)
    assert "must_change_password" in gate and "403" in gate and "password_change_required" in gate
