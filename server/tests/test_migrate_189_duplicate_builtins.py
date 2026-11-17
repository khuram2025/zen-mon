"""migrate-189 on appliances with several built-in templates of one name (disposable PostgreSQL).

Uniqueness of device_profiles is (name, version). A seed migration applied again after a
later migration bumped its row's version (063 then 069 for 'Aruba Wireless Controller')
leaves an old-version and a new-version built-in row with the same name. The first cut of
189 gave every such row the same builtin_key and failed on the unique index; production
appliances upgrading to 1.25.10 stopped there (1.25.6 -> 1.25.10, step run_migration):

    ERROR: duplicate key value violates unique constraint "device_profiles_builtin_key"
    DETAIL: Key (builtin_key)=(aruba_wlc) already exists.

The tests seed duplicates for Aruba and FortiGate into a database built without 189 and
upgrade it through the real runner, from scratch and from the state the failed appliances
are in (every statement before the keyed UPDATE applied, nothing recorded). An appliance
where the first cut succeeded reconciles the old checksum instead of re-running the file.
"""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest

from pg_fixture import (  # noqa: F401  (fixtures are used by name)
    SCRIPTS_DIR,
    PsqlError,
    copy_migration_set,
    pg_cluster,
    pg_db,
)

M189 = "migrate-189-device-templates.sql"
LATER = ["migrate-190-discovery-rule-engine.sql", "migrate-191-discovery-rules-inventory.sql",
         "migrate-192-device-groups.sql"]
SHIPPED_CHECKSUM = "dd05631ff051784b9b2d9ba12799003bbeec0fb958dc24d290a19fb86ce380d4"   # 1.25.10 - 1.25.12
ARUBA, FORTI = "Aruba Wireless Controller", "Fortinet FortiGate"

# The statement of the first cut (as shipped in 1.25.10 - 1.25.12), reduced to two names.
FIRST_CUT_KEYING = """
UPDATE device_profiles p
   SET builtin_key = v.k,
       device_type = COALESCE(p.device_type, v.t)
  FROM (VALUES
        ('Fortinet FortiGate',               'fortinet_fortigate',   'firewall'),
        ('Aruba Wireless Controller',        'aruba_wlc',            'wireless_controller')
       ) AS v(n, k, t)
 WHERE p.name = v.n AND p.builtin AND p.builtin_key IS NULL
   AND NOT EXISTS (SELECT 1 FROM device_profiles o WHERE o.builtin_key = v.k);
"""


def _before_keying() -> str:
    """Every statement of 189 that runs before the keyed UPDATE (unchanged by the revision)."""
    text = (SCRIPTS_DIR / M189).read_text()
    head, sep, _ = text.partition("-- Built-in identity and default device types.")
    assert sep, "marker comment moved; keep the half-applied state test in step"
    return head


def _seed(db) -> SimpleNamespace:
    """An old-version copy of the Aruba and FortiGate built-ins; devices on the OLD Aruba row only."""
    ids = SimpleNamespace()
    for label, name in (("aruba", ARUBA), ("forti", FORTI)):
        current = db.scalar("SELECT id FROM device_profiles WHERE name = $1 AND builtin ORDER BY version DESC LIMIT 1", name)
        assert current, f"seed migrations no longer create {name}"
        old = db.scalar("""
            INSERT INTO device_profiles (name, vendor, description, version, builtin, match_rules, oid_groups)
            SELECT name, vendor, description, version - 1, TRUE, match_rules, oid_groups
              FROM device_profiles WHERE id = $1 RETURNING id""", current)
        setattr(ids, f"{label}_new", str(current))
        setattr(ids, f"{label}_old", str(old))
    ids.devices = [str(db.scalar("INSERT INTO devices (hostname, ip_address, profile_id) VALUES ($1, $2::inet, $3) RETURNING id",
                                 f"wlc-{n}", f"10.9.0.{n}", ids.aruba_old)) for n in (1, 2)]
    return ids


def _pre_189(pg_cluster, tmp_path_factory, label):
    scripts, lock = copy_migration_set(tmp_path_factory.mktemp(label), exclude=[M189, *LATER])
    db = pg_cluster.create_database(f"zp_m189_{label}_{uuid.uuid4().hex[:8]}", template=None)
    pg_cluster.replay_migrations(db.name, scripts_dir=scripts, lock=lock)
    assert db.scalar("SELECT to_regclass('device_types')") is None
    return db


def _keys(db, name):
    return {str(r[0]): r[1] for r in db.query("SELECT id, builtin_key FROM device_profiles WHERE name = $1 AND builtin", name)}


def _assert_converged(db, ids, output):
    assert f"apply   {M189}" in output
    assert db.scalar("SELECT checksum FROM schema_migrations WHERE filename = $1", M189) == \
        hashlib.sha256((SCRIPTS_DIR / M189).read_bytes()).hexdigest()
    aruba, forti = _keys(db, ARUBA), _keys(db, FORTI)
    # The row the devices use keeps the canonical key; with no devices, the highest version does.
    assert aruba[ids.aruba_old] == "aruba_wlc" and aruba[ids.aruba_new].startswith("aruba_wireless_controller_")
    assert forti[ids.forti_new] == "fortinet_fortigate" and forti[ids.forti_old].startswith("fortinet_fortigate_")
    assert db.scalar("SELECT count(*) FROM device_profiles WHERE builtin AND builtin_key IS NULL") == 0
    # Both rows get the default type; no device moved; every row has its baseline revision.
    assert {r[0] for r in db.query("SELECT device_type FROM device_profiles WHERE name = $1", ARUBA)} == {"wireless_controller"}
    assert {r[0] for r in db.query("SELECT device_type FROM device_profiles WHERE name = $1", FORTI)} == {"firewall"}
    assert {str(r[0]) for r in db.query("SELECT profile_id FROM devices WHERE id = ANY($1::uuid[])", ids.devices)} == {ids.aruba_old}
    assert db.scalar("SELECT count(*) FROM device_profiles p WHERE NOT EXISTS ("
                     "SELECT 1 FROM device_profile_versions v WHERE v.profile_id = p.id AND v.action = 'baseline')") == 0
    # Later migrations ran on top, and replaying 189 changes nothing.
    assert db.scalar("SELECT to_regclass('device_group_members')") is not None
    before = (aruba, forti)
    db.psql_file(SCRIPTS_DIR / M189)
    assert (_keys(db, ARUBA), _keys(db, FORTI)) == before


def test_the_first_cut_fails_on_duplicate_names(pg_cluster, tmp_path_factory):
    """The fixture reproduces the field failure, so the tests below exercise the real case."""
    db = _pre_189(pg_cluster, tmp_path_factory, "repro")
    try:
        _seed(db)
        db.psql(_before_keying())
        with pytest.raises(PsqlError, match="device_profiles_builtin_key"):
            db.psql(FIRST_CUT_KEYING)
    finally:
        db.drop()


def test_upgrade_from_scratch_keys_one_row_per_name(pg_cluster, tmp_path_factory):
    db = _pre_189(pg_cluster, tmp_path_factory, "scratch")
    try:
        ids = _seed(db)
        output = pg_cluster.replay_migrations(db.name)
        _assert_converged(db, ids, output)
    finally:
        db.drop()


def test_upgrade_converges_from_the_failed_first_cut(pg_cluster, tmp_path_factory):
    """psql stopped at the keyed UPDATE: types, columns and the unique index exist, no key is set,
    nothing is recorded in schema_migrations, device_profile_versions does not exist yet."""
    db = _pre_189(pg_cluster, tmp_path_factory, "halfway")
    try:
        ids = _seed(db)
        db.psql(_before_keying())
        with pytest.raises(PsqlError):
            db.psql(FIRST_CUT_KEYING)
        assert db.scalar("SELECT to_regclass('device_profiles_builtin_key')") is not None
        assert db.scalar("SELECT to_regclass('device_profile_versions')") is None
        assert db.scalar("SELECT count(*) FROM schema_migrations WHERE filename = $1", M189) == 0
        output = pg_cluster.replay_migrations(db.name)
        _assert_converged(db, ids, output)
    finally:
        db.drop()


def test_an_appliance_that_ran_the_first_cut_reconciles_without_rerunning(pg_cluster, pg_db):
    """One row per name: the first cut succeeded and left what the revision leaves."""
    pg_db.execute("UPDATE schema_migrations SET checksum = $2 WHERE filename = $1", M189, SHIPPED_CHECKSUM)
    keys = pg_db.query("SELECT id, builtin_key FROM device_profiles WHERE builtin ORDER BY id")
    output = pg_cluster.replay_migrations(pg_db.name)
    assert f"reconcile {M189}" in output and f"apply   {M189}" not in output
    assert pg_db.scalar("SELECT checksum FROM schema_migrations WHERE filename = $1", M189) != SHIPPED_CHECKSUM
    assert pg_db.query("SELECT id, builtin_key FROM device_profiles WHERE builtin ORDER BY id") == keys
