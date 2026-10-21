"""No installation path may leave a known admin password (ZP-06).

init-postgres.sql seeds the admin account locked. install.sh and the appliance
first boot give it a random one-time password, stored only in a root-only file
and flagged must_change_password. The retired admin/admin123 seed hash is still
recognised: migration 153 (append-only) flags accounts that carry it, and
install.sh replaces it with a one-time password on a re-run.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEED_HASH = re.compile(r"\$2b\$12\$vjHI8XBgL\.dCyn\.sgl41VufIFkQGcEzjt78GJdB66AwG9e9MZasai")
MIGRATION = ROOT / "scripts" / "migrate-153-force-default-admin-password-change.sql"


def read(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_the_seed_is_locked_and_carries_no_known_hash():
    seed = read("scripts/init-postgres.sql")
    insert = re.search(r"INSERT INTO users \(username, email, password_hash, full_name, role\)\s*"
                       r"VALUES \('admin', 'admin@zenplus.local',\s*'([^']+)'", seed)
    assert insert and insert.group(1) == "!locked"
    assert not SEED_HASH.search(seed)
    assert "admin123" not in seed


def test_the_locked_seed_cannot_sign_in():
    from app.core.security import verify_password
    assert verify_password("admin123", "!locked") is False
    assert verify_password("", "!locked") is False


def test_the_retired_hash_is_still_recognised_where_it_matters():
    """install.sh and migration 153 know the old seed (appliances installed before)."""
    for relative in ("install.sh", MIGRATION.relative_to(ROOT).as_posix()):
        assert SEED_HASH.search(read(relative)), f"{relative} does not recognise the retired seed hash"
    from passlib.context import CryptContext
    assert CryptContext(schemes=["bcrypt"]).verify("admin123", SEED_HASH.search(read("install.sh")).group(0))


def test_the_vmware_first_boot_seed_rewrite_still_matches():
    pattern = r"(VALUES \('admin', 'admin@zenplus.local',\s*)'[^']+'"
    assert pattern in read("appliance/vmware/first-boot.py")
    assert re.search(pattern, read("scripts/init-postgres.sql"))


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


def test_the_installer_sets_a_one_time_password_only_for_locked_seeded_or_corrupt_accounts():
    script = read("install.sh")
    body = re.search(r"\nset_initial_admin_password\(\) \{\n(.*?)\n\}\n", script, re.S).group(1)
    update = re.search(r"UPDATE users\n(.*?)WHERE username = 'admin';", body, re.S).group(1)
    assert "password_hash = :'admin_hash'" in update and "must_change_password = TRUE" in update
    assert "secrets.token_urlsafe" in body
    # file is root-only and only moved into place after the database update
    assert body.index("UPDATE users") < body.index('mv -f "$tmp" "$ADMIN_PASSWORD_FILE"')
    assert 'chmod 0600 "$tmp"' in body
    assert "ADMIN_PASSWORD_FILE=/var/lib/zenplus/initial-admin-password" in script
    branch = re.search(r'elif \[\[ "\$stored" == \'!locked\' \|\| "\$stored" == "\$RETIRED_ADMIN_SEED_HASH" '
                       r'\|\| "\$stored" != \'\$2\'\*\'\$\'\* \]\]; then(.*?)\n    else\n(.*?)\n    fi\n',
                       script, re.S)
    assert branch, "one-time password only for a locked, retired-seed or corrupt hash"
    assert "set_initial_admin_password" in branch.group(1)
    assert "ADMIN_PASSWORD_STATE=existing" in branch.group(2)
    assert "set_initial_admin_password" not in branch.group(2)


def test_the_installer_never_prints_or_falls_back_to_a_known_password():
    script = read("install.sh")
    assert "admin123" not in re.sub(r"#.*", "", script)
    summary = re.search(r"\nprint_summary\(\) \{\n(.*?)\n\}\n", script, re.S).group(1)
    assert "ADMIN_PASSWORD_FILE" in summary
    assert "password}" not in summary and "$password" not in summary


def test_set_initial_admin_password_writes_a_root_only_file_after_the_database(tmp_path):
    """Run the real function with psql/python stubbed."""
    definitions = tmp_path / "defs.sh"
    definitions.write_text(read("install.sh").rsplit('\nmain "$@"', 1)[0])
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python3").write_text("#!/bin/sh\necho 'Abcdefghijklmnopqrst $2b$12$hashhashhash'\n")
    (venv / "python3").chmod(0o755)
    sql_log = tmp_path / "sql.log"
    pw_file = tmp_path / "state" / "initial-admin-password"
    script = f"""
source {shlex.quote(str(definitions))}
ZENPLUS_HOME={shlex.quote(str(tmp_path))}
ADMIN_PASSWORD_FILE={shlex.quote(str(pw_file))}
install() {{ command install -d -m 0750 "${{@: -1}}"; }}
runuser() {{ shift 3; echo "$*" >> {shlex.quote(str(sql_log))}; cat >> {shlex.quote(str(sql_log))}; return $PSQL_RC; }}
set_initial_admin_password; rc=$?
echo "rc=$rc state=$ADMIN_PASSWORD_STATE"
"""
    env = dict(os.environ, INSTALL_LOG=str(tmp_path / "install.log"), PSQL_RC="0")
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert "rc=0 state=initial" in result.stdout, result.stdout + result.stderr
    assert "Abcdefghijklmnopqrst" not in result.stdout + result.stderr, "password must not be printed"
    assert "Password: Abcdefghijklmnopqrst" in pw_file.read_text()
    assert (pw_file.stat().st_mode & 0o777) == 0o600
    sql = sql_log.read_text()
    assert "admin_hash=$2b$12$hashhashhash" in sql and "must_change_password = TRUE" in sql

    pw_file.unlink()
    env["PSQL_RC"] = "1"
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert "rc=1 state=unknown" in result.stdout, result.stdout + result.stderr
    assert not pw_file.exists() and not list(pw_file.parent.iterdir()), "no stale password file on failure"


def test_the_api_refuses_everything_but_a_password_change_to_a_flagged_account():
    security = read("server/app/core/security.py")
    gate = re.search(r"async def _enforce_sign_in_obligations(.*?)\n\n\nasync def", security, re.S).group(1)
    assert "must_change_password" in gate and "403" in gate and "password_change_required" in gate
