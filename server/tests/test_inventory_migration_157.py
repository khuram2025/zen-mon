"""SQL-level tests for migrate-157-inventory-foundation.sql.

Everything runs on the private PostgreSQL cluster from ``pg_fixture`` (never the
appliance database): fresh replay, re-apply, an N-1 -> N upgrade, and the
behaviour of every trigger the migration installs. Only the lint test needs no
database.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import pg_fixture
from pg_fixture import (  # noqa: F401  (fixtures are used by name)
    REPO_ROOT,
    SCRIPTS_DIR,
    PgDatabase,
    copy_migration_set,
    pg_cluster,
    pg_db,
    pg_db_module,
)

MIGRATION = "migrate-157-inventory-foundation.sql"
MIGRATION_PATH = SCRIPTS_DIR / MIGRATION

EVENT_KEYS = ("category", "action", "subject", "subject_key", "old_value", "new_value",
              "asset_kind", "asset_id", "asset_name", "detail")

DEVICE_IDENTITY_COLUMNS = ("vendor", "model", "os_version", "firmware_version",
                           "serial_number", "sys_object_id", "sys_descr")
SERVER_IDENTITY_COLUMNS = ("os_name", "os_version", "kernel_or_build", "architecture",
                           "cpu_model", "hw_manufacturer", "hw_model", "hw_serial")
TEMPLATE_IDENTITY_KEYS = ("fgt_fw_version", "pan_sw_version", "f5_version", "f5_serial")


# ------------------------------------------------------------------ helpers

_seq = itertools.count(1)


def add_server(db: PgDatabase, name: str | None = None, **cols) -> uuid.UUID:
    names = ["display_name", *cols]
    marks = ", ".join(f"${i + 1}" for i in range(len(names)))
    return db.scalar(
        f"INSERT INTO servers ({', '.join(names)}) VALUES ({marks}) RETURNING id",
        name or f"srv-{next(_seq)}", *cols.values())


def add_device(db: PgDatabase, hostname: str | None = None, **cols) -> uuid.UUID:
    n = next(_seq)
    cols.setdefault("ip_address", f"10.77.{n // 250}.{n % 250 + 1}")
    names = ["hostname", *cols]
    marks = ", ".join(f"${i + 1}" + ("::inet" if name == "ip_address" else "")
                      for i, name in enumerate(names))
    return db.scalar(
        f"INSERT INTO devices ({', '.join(names)}) VALUES ({marks}) RETURNING id",
        hostname or f"dev-{n}", *cols.values())


def add_software(db: PgDatabase, server_id, package: str, version: str | None = "1.0", **cols) -> None:
    names = ["server_id", "package_name", "version", *cols]
    marks = ", ".join(f"${i + 1}" for i in range(len(names)))
    db.execute(f"INSERT INTO server_software_inventory ({', '.join(names)}) VALUES ({marks})",
               server_id, package, version, *cols.values())


def baseline(db: PgDatabase, server_id) -> None:
    db.execute("UPDATE servers SET inventory_baselined_at = now() WHERE id = $1", server_id)


def events(db: PgDatabase, asset_id=None) -> list[dict]:
    sql = f"SELECT {', '.join(EVENT_KEYS)} FROM inventory_change_events"
    args: tuple = ()
    if asset_id is not None:
        sql += " WHERE asset_id = $1"
        args = (asset_id,)
    return [dict(zip(EVENT_KEYS, row)) for row in db.query(sql + " ORDER BY id", *args)]


def identity(db: PgDatabase, kind: str, asset_id) -> dict | None:
    rows = db.query(
        "SELECT norm_rev, source_changed_at, os_version FROM inventory_identity "
        "WHERE asset_kind = $1 AND asset_id = $2", kind, asset_id)
    return dict(zip(("norm_rev", "source_changed_at", "os_version"), rows[0])) if rows else None


def mark_clean(db: PgDatabase, kind: str, asset_id, rev: int = 1) -> None:
    db.execute("UPDATE inventory_identity SET norm_rev = $3, normalized_at = now() "
               "WHERE asset_kind = $1 AND asset_id = $2", kind, asset_id, rev)


def is_dirty(db: PgDatabase, kind: str, asset_id) -> bool:
    row = identity(db, kind, asset_id)
    assert row is not None, f"no identity row for {kind} {asset_id}"
    return row["norm_rev"] is None


def revisions(db: PgDatabase, kind: str, asset_id) -> tuple:
    """(norm_rev, values_rev) of one identity row."""
    return db.query("SELECT norm_rev, values_rev FROM inventory_identity "
                    "WHERE asset_kind = $1 AND asset_id = $2", kind, asset_id)[0]


def write_identity_like_ingest(db: PgDatabase, server_id, rev: int, os_version, hw_serial=None, hw_model=None) -> None:
    """The agent ingest's shape: one upsert storing the values and both revisions."""
    db.execute(
        "INSERT INTO inventory_identity (asset_kind, asset_id, os_product, os_version, hw_serial, hw_model, "
        "norm_rev, values_rev, normalized_at) VALUES ('server', $1, 'Windows Server', $2, $3, $4, $5, $5, now()) "
        "ON CONFLICT (asset_kind, asset_id) DO UPDATE SET os_product = EXCLUDED.os_product, "
        "os_version = EXCLUDED.os_version, hw_serial = EXCLUDED.hw_serial, hw_model = EXCLUDED.hw_model, "
        "norm_rev = EXCLUDED.norm_rev, values_rev = EXCLUDED.values_rev, normalized_at = now()",
        server_id, os_version, hw_serial, hw_model, rev)


def write_identity_like_the_loop(db: PgDatabase, server_id, rev: int, os_version, hw_serial=None, hw_model=None) -> None:
    """The normalise loop's shape: an UPDATE of the existing row, capture left on
    (the dirty phase runs unsuppressed, so the trigger alone must keep it quiet)."""
    db.execute(
        "UPDATE inventory_identity SET os_product = 'Windows Server', os_version = $2, hw_serial = $3, "
        "hw_model = $4, norm_rev = $5, values_rev = $5, normalized_at = now() "
        "WHERE asset_kind = 'server' AND asset_id = $1",
        server_id, os_version, hw_serial, hw_model, rev)


IDENTITY_WRITERS = {"ingest": write_identity_like_ingest, "loop": write_identity_like_the_loop}


def plpgsql_calls(db: PgDatabase, function: str, statement: str) -> int:
    """Run ``statement`` in one transaction and return how many times PL/pgSQL
    ``function`` was entered, as counted by the server itself."""
    out = db.psql(
        "BEGIN;\nSET LOCAL track_functions = 'pl';\n"
        f"{statement};\n"
        "SELECT COALESCE((SELECT calls FROM pg_stat_xact_user_functions "
        f"WHERE funcname = '{function}'), 0);\nCOMMIT;")
    return int(out.strip().splitlines()[-1])


def schema_fingerprint(db: PgDatabase) -> str:
    return db.scalar(
        """
        SELECT md5(string_agg(x, '|' ORDER BY x)) FROM (
          SELECT 'col:' || table_name || '.' || column_name || ':' || data_type || ':'
                 || is_nullable || ':' || COALESCE(column_default, '') AS x
            FROM information_schema.columns
           WHERE table_schema = 'public'
             AND (table_name LIKE 'inventory\\_%'
                  OR table_name IN ('devices', 'servers', 'server_software_inventory'))
          UNION ALL
          SELECT 'idx:' || indexdef FROM pg_indexes
           WHERE schemaname = 'public'
             AND (tablename LIKE 'inventory\\_%'
                  OR tablename IN ('devices', 'servers', 'server_software_inventory'))
          UNION ALL
          SELECT 'trg:' || pg_get_triggerdef(t.oid) FROM pg_trigger t
           WHERE NOT t.tgisinternal AND t.tgname LIKE 'inventory\\_%'
          UNION ALL
          SELECT 'fn:' || pg_get_functiondef(p.oid) FROM pg_proc p
           WHERE p.proname LIKE 'inventory\\_%'
        ) s
        """)


# ------------------------------------------- (1) fresh replay creates objects

EXPECTED_COLUMNS = {
    "devices": {"sys_descr": "text", "identity_updated_at": "timestamp with time zone"},
    "servers": {
        "inventory_updated_at": "timestamp with time zone",
        "inventory_collected_at": "timestamp with time zone",
        "inventory_baselined_at": "timestamp with time zone",
        "hw_manufacturer": "character varying", "hw_model": "character varying",
        "hw_serial": "character varying", "bios_version": "character varying",
        "bios_date": "date", "virtualization": "character varying",
        "software_truncated": "boolean",
    },
    "server_software_inventory": {
        "architecture": "character varying", "source": "character varying",
        "install_location": "text", "first_seen_at": "timestamp with time zone",
        "name_key": "character varying", "publisher_key": "character varying",
        "category": "character varying", "norm_rev": "integer",
    },
    "inventory_identity": {
        "asset_kind": "character varying", "asset_id": "uuid", "vendor_key": "character varying",
        "vendor_label": "character varying", "os_family": "character varying",
        "os_product": "character varying", "os_release": "character varying",
        "os_version": "character varying", "os_build": "character varying",
        "os_edition": "character varying", "os_source": "character varying",
        "hw_vendor": "character varying", "hw_model": "character varying",
        "hw_serial": "character varying", "hw_source": "character varying",
        "lifecycle_slug": "character varying", "norm_rev": "integer", "values_rev": "integer",
        "source_changed_at": "timestamp with time zone", "normalized_at": "timestamp with time zone",
    },
    "inventory_change_events": {
        "id": "bigint", "observed_at": "timestamp with time zone", "asset_kind": "character varying",
        "asset_id": "uuid", "asset_name": "character varying", "category": "character varying",
        "action": "character varying", "subject": "character varying",
        "subject_key": "character varying", "old_value": "text", "new_value": "text",
        "detail": "jsonb",
    },
    "inventory_daily_snapshots": {
        "day": "date", "metric": "character varying", "dimension": "character varying",
        "value": "bigint",
    },
}

EXPECTED_INDEXES = {
    "idx_ssi_name_key", "idx_ssi_publisher", "idx_ssi_unkeyed",
    "idx_inv_identity_os", "idx_inv_identity_hw", "idx_inv_identity_dirty",
    "idx_inv_events_time", "idx_inv_events_asset", "idx_inv_events_cat", "idx_inv_events_key",
}

# (table, trigger) -> must carry a WHEN clause so non-matching rows never enter PL/pgSQL
EXPECTED_TRIGGERS = {
    ("devices", "inventory_devices_seed_ins"): False,
    ("devices", "inventory_devices_dirty_upd"): True,
    ("devices", "inventory_devices_identity_del"): False,
    ("servers", "inventory_servers_seed_ins"): False,
    ("servers", "inventory_servers_dirty_upd"): True,
    ("servers", "inventory_servers_identity_del"): False,
    ("device_template_values", "inventory_dtv_dirty_ins"): True,
    ("device_template_values", "inventory_dtv_dirty_upd"): True,
    ("server_software_inventory", "inventory_ssi_added"): False,
    ("server_software_inventory", "inventory_ssi_changed"): True,
    ("server_software_inventory", "inventory_ssi_removed"): False,
    ("inventory_identity", "inventory_identity_changed"): True,
}


def test_fresh_replay_creates_every_object(pg_db_module):
    db = pg_db_module
    assert db.scalar("SELECT count(*) FROM schema_migrations WHERE filename = $1", MIGRATION) == 1

    for table, expected in EXPECTED_COLUMNS.items():
        actual = dict(db.query(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1", table))
        missing = {c: t for c, t in expected.items() if actual.get(c) != t}
        assert not missing, f"{table}: missing or mistyped columns {missing}"
    for table in ("inventory_identity", "inventory_change_events", "inventory_daily_snapshots"):
        actual = {r[0] for r in db.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1", table)}
        assert actual == set(EXPECTED_COLUMNS[table]), f"{table}: unexpected column set"

    flags = {r[0]: (r[1], r[2]) for r in db.query(
        "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND "
        "(table_name, column_name) IN (('servers', 'software_truncated'), "
        "('server_software_inventory', 'first_seen_at'))")}
    assert flags["software_truncated"] == ("NO", "false")
    assert flags["first_seen_at"] == ("NO", "now()")

    lengths = dict(db.query(
        "SELECT table_name || '.' || column_name, character_maximum_length FROM information_schema.columns "
        "WHERE table_schema = 'public' AND (table_name, column_name) IN "
        "(('servers', 'hw_manufacturer'), ('servers', 'hw_model'), ('servers', 'hw_serial'), "
        "('server_software_inventory', 'name_key'), ('server_software_inventory', 'publisher_key'), "
        "('server_software_inventory', 'architecture'), ('inventory_change_events', 'subject'))"))
    assert lengths == {
        "servers.hw_manufacturer": 128, "servers.hw_model": 255, "servers.hw_serial": 128,
        "server_software_inventory.name_key": 160, "server_software_inventory.publisher_key": 96,
        "server_software_inventory.architecture": 16, "inventory_change_events.subject": 255,
    }

    indexes = {r[0] for r in db.query("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
    assert EXPECTED_INDEXES <= indexes
    dirty_idx = db.scalar("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_inv_identity_dirty'")
    assert "WHERE (norm_rev IS NULL)" in dirty_idx
    unkeyed_idx = db.scalar("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_ssi_unkeyed'")
    assert "(server_id, package_name) WHERE (norm_rev IS NULL)" in unkeyed_idx

    pk = [r[0] for r in db.query(
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "AND a.attnum = ANY (i.indkey) WHERE i.indrelid = 'inventory_identity'::regclass "
        "AND i.indisprimary ORDER BY array_position(i.indkey::int2[], a.attnum)")]
    assert pk == ["asset_kind", "asset_id"]
    assert db.scalar("SELECT count(*) FROM pg_constraint WHERE conrelid = 'inventory_change_events'::regclass "
                     "AND contype = 'f'") == 0, "change events must not hold foreign keys"

    triggers = {(r[0], r[1]): r[2] for r in db.query(
        "SELECT c.relname, t.tgname, t.tgqual IS NOT NULL FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid WHERE NOT t.tgisinternal AND t.tgname LIKE 'inventory\\_%'")}
    assert triggers == EXPECTED_TRIGGERS

    functions = {r[0] for r in db.query("SELECT proname FROM pg_proc WHERE proname LIKE 'inventory\\_%'")}
    assert functions == {"inventory_identity_mark_dirty", "inventory_identity_mark_dirty_from_template",
                         "inventory_identity_remove", "inventory_software_capture",
                         "inventory_identity_capture"}

    # The migration's own seed ran against the devices that init/seed created.
    assert db.scalar("SELECT count(*) FROM devices") > 0
    assert db.scalar("SELECT count(*) FROM devices d WHERE NOT EXISTS (SELECT 1 FROM inventory_identity i "
                     "WHERE i.asset_kind = 'device' AND i.asset_id = d.id AND i.norm_rev IS NULL)") == 0
    assert db.scalar("SELECT count(*) FROM inventory_change_events") == 0


def test_table_constraints(pg_db):
    sid = add_server(pg_db)
    with pytest.raises(asyncpg.CheckViolationError):
        pg_db.execute("INSERT INTO inventory_change_events (asset_kind, asset_id, category, action, subject) "
                      "VALUES ('server', $1, 'nonsense', 'added', 'x')", sid)
    with pytest.raises(asyncpg.CheckViolationError):
        pg_db.execute("INSERT INTO inventory_change_events (asset_kind, asset_id, category, action, subject) "
                      "VALUES ('server', $1, 'software', 'renamed', 'x')", sid)
    with pytest.raises(asyncpg.CheckViolationError):
        pg_db.execute("INSERT INTO inventory_identity (asset_kind, asset_id) VALUES ('printer', $1)", sid)
    pg_db.execute("INSERT INTO inventory_daily_snapshots (day, metric, value) VALUES (current_date, 'assets', 1)")
    with pytest.raises(asyncpg.UniqueViolationError):
        pg_db.execute("INSERT INTO inventory_daily_snapshots (day, metric, value) VALUES (current_date, 'assets', 2)")


# --------------------------------------------- (2) second application is a no-op

def test_applying_the_migration_twice_is_a_clean_no_op(pg_db):
    sid = add_server(pg_db, "twice")
    add_software(pg_db, sid, "App", "1.0")
    baseline(pg_db, sid)
    add_software(pg_db, sid, "Added After Baseline", "2.0")
    mark_clean(pg_db, "server", sid, rev=7)
    events_before = events(pg_db)
    assert len(events_before) == 1
    fingerprint = schema_fingerprint(pg_db)
    identity_rows = pg_db.scalar("SELECT count(*) FROM inventory_identity")

    result = pg_db.run_psql(file=MIGRATION_PATH)

    assert result.returncode == 0
    assert "ERROR" not in result.stderr
    assert schema_fingerprint(pg_db) == fingerprint
    assert events(pg_db) == events_before, "re-applying must not add or rewrite history"
    assert pg_db.scalar("SELECT count(*) FROM inventory_identity") == identity_rows
    assert identity(pg_db, "server", sid)["norm_rev"] == 7, "the seed must not re-dirty a normalised row"
    assert pg_db.scalar("SELECT inventory_baselined_at IS NOT NULL FROM servers WHERE id = $1", sid)


def test_reapplying_to_a_database_that_ran_an_earlier_cut_adds_what_is_missing(pg_cluster, pg_db):
    """The columns and the index came after the first cut of this file: psql-ing
    the file over a database that has the older shape must add them, replace the
    triggers and leave every row and event alone."""
    expected = schema_fingerprint(pg_db)
    sid = add_server(pg_db, "older-cut")
    add_software(pg_db, sid, "App", "1.0")
    baseline(pg_db, sid)
    add_software(pg_db, sid, "Late", "2.0")
    mark_clean(pg_db, "server", sid, rev=3)
    events_before = events(pg_db)
    assert len(events_before) == 1

    pg_db.execute("""
        ALTER TABLE inventory_identity DROP COLUMN values_rev CASCADE;
        ALTER TABLE servers DROP COLUMN inventory_collected_at;
        DROP INDEX idx_ssi_unkeyed;
        DROP TRIGGER inventory_ssi_changed ON server_software_inventory;
        CREATE TRIGGER inventory_ssi_changed AFTER UPDATE OF version ON server_software_inventory
            FOR EACH ROW WHEN (OLD.version IS DISTINCT FROM NEW.version)
            EXECUTE FUNCTION inventory_software_capture();
        CREATE TRIGGER inventory_identity_changed AFTER UPDATE OF os_version, hw_serial, hw_model
            ON inventory_identity FOR EACH ROW
            WHEN (OLD.os_version IS NOT NULL AND NEW.os_version IS NOT NULL AND OLD.os_version <> NEW.os_version)
            EXECUTE FUNCTION inventory_identity_capture();
    """)
    assert schema_fingerprint(pg_db) != expected, "the downgrade did not take, so the test proves nothing"

    result = pg_db.run_psql(file=MIGRATION_PATH)

    assert result.returncode == 0 and "ERROR" not in result.stderr
    assert schema_fingerprint(pg_db) == expected
    assert events(pg_db) == events_before
    assert identity(pg_db, "server", sid)["norm_rev"] == 3
    assert revisions(pg_db, "server", sid) == (3, None), "an existing row has no recorded pack revision yet"
    assert pg_db.scalar("SELECT inventory_collected_at FROM servers WHERE id = $1", sid) is None

    # The replacement triggers are the new ones.
    pg_db.execute("UPDATE server_software_inventory SET version = NULL WHERE server_id = $1 AND package_name = 'App'", sid)
    assert events(pg_db) == events_before, "NULL version must be a gap again"
    pg_db.execute("UPDATE inventory_identity SET os_version = '1.0', values_rev = 1 WHERE asset_kind = 'server' AND asset_id = $1", sid)
    pg_db.execute("UPDATE inventory_identity SET os_version = '2.0', values_rev = 2 WHERE asset_kind = 'server' AND asset_id = $1", sid)
    assert events(pg_db) == events_before, "a revision change must not read as a change"


# ----------------------------------------------- (3) N-1 -> N upgrade

@pytest.fixture(scope="module")
def upgraded(pg_cluster, tmp_path_factory):
    """A database built from every migration except 157, populated the way a
    running appliance would be, then upgraded through run-migrations.py."""
    scripts, lock = copy_migration_set(tmp_path_factory.mktemp("n_minus_1"), exclude=[MIGRATION])
    db = pg_cluster.create_database(f"zp_n1_{uuid.uuid4().hex[:8]}", template=None)
    try:
        pg_cluster.replay_migrations(db.name, scripts_dir=scripts, lock=lock)
        assert db.scalar("SELECT to_regclass('inventory_change_events')") is None
        assert db.scalar("SELECT to_regclass('inventory_identity')") is None

        with_software = db.scalar("INSERT INTO servers (display_name) VALUES ('n1-with-software') RETURNING id")
        no_software = db.scalar("INSERT INTO servers (display_name) VALUES ('n1-no-software') RETURNING id")
        also_software = db.scalar("INSERT INTO servers (display_name) VALUES ('n1-also-software') RETURNING id")
        for pkg, ver in (("Chrome", "129.0"), ("7-Zip", "23.01"), ("Zabbix Agent", "6.0.1")):
            db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, vendor) "
                       "VALUES ($1, $2, $3, 'v')", with_software, pkg, ver)
        db.execute("INSERT INTO server_software_inventory (server_id, package_name, version) "
                   "VALUES ($1, 'Notepad++', '8.6')", also_software)
        fortigate = db.scalar("INSERT INTO devices (hostname, ip_address, vendor, model, os_version) "
                              "VALUES ('n1-fgt', '10.99.0.1', 'Fortinet', 'FG-100F', 'v7.2.5') RETURNING id")
        bare = db.scalar("INSERT INTO devices (hostname, ip_address) VALUES ('n1-bare', '10.99.0.2') RETURNING id")

        servers_before = db.scalar("SELECT count(*) FROM servers")
        devices_before = db.scalar("SELECT count(*) FROM devices")
        output = pg_cluster.replay_migrations(db.name)  # the real upgrade path

        yield SimpleNamespace(
            db=db, output=output, servers_before=servers_before, devices_before=devices_before,
            with_software=with_software, no_software=no_software, also_software=also_software,
            fortigate=fortigate, bare=bare,
            event_count_after_upgrade=db.scalar("SELECT count(*) FROM inventory_change_events"),
        )
    finally:
        db.drop()


def test_upgrade_applies_154_and_seeds_without_emitting_history(upgraded):
    db = upgraded.db
    assert "apply   migrate-157-inventory-foundation.sql" in upgraded.output
    assert db.scalar("SELECT count(*) FROM schema_migrations WHERE filename = $1", MIGRATION) == 1

    assert upgraded.event_count_after_upgrade == 0

    # Every pre-existing asset has an identity row, all dirty.
    assert db.scalar("SELECT count(*) FROM inventory_identity WHERE asset_kind = 'server'") == upgraded.servers_before
    assert db.scalar("SELECT count(*) FROM inventory_identity WHERE asset_kind = 'device'") == upgraded.devices_before
    assert db.scalar("SELECT count(*) FROM inventory_identity WHERE norm_rev IS NOT NULL") == 0
    assert db.scalar("SELECT count(*) FROM servers s WHERE NOT EXISTS (SELECT 1 FROM inventory_identity i "
                     "WHERE i.asset_kind = 'server' AND i.asset_id = s.id)") == 0
    assert db.scalar("SELECT count(*) FROM devices d WHERE NOT EXISTS (SELECT 1 FROM inventory_identity i "
                     "WHERE i.asset_kind = 'device' AND i.asset_id = d.id)") == 0

    # Only servers that already hold software are baselined.
    baselined = dict(db.query("SELECT display_name, inventory_baselined_at IS NOT NULL FROM servers"))
    assert baselined == {"n1-with-software": True, "n1-no-software": False, "n1-also-software": True}

    # Rows that existed before get a first_seen_at, the new text columns stay empty.
    assert db.scalar("SELECT count(*) FROM server_software_inventory WHERE first_seen_at IS NULL") == 0
    assert db.scalar("SELECT count(*) FROM server_software_inventory WHERE name_key IS NOT NULL") == 0
    assert db.scalar("SELECT bool_and(NOT software_truncated) FROM servers")
    # No stored identity value pre-dates the rule pack, and no snapshot has been accepted yet.
    assert db.scalar("SELECT count(*) FROM inventory_identity WHERE values_rev IS NOT NULL") == 0
    assert db.scalar("SELECT count(*) FROM servers WHERE inventory_collected_at IS NOT NULL") == 0


def test_after_upgrade_the_baselined_gate_decides_what_is_recorded(upgraded):
    db = upgraded.db
    start = len(events(db))

    # Baselined by the seed: its next upload diffs normally.
    db.execute("UPDATE server_software_inventory SET version = '130.0' "
               "WHERE server_id = $1 AND package_name = 'Chrome'", upgraded.with_software)
    add_software(db, upgraded.with_software, "Wireshark", "4.2")
    db.execute("DELETE FROM server_software_inventory WHERE server_id = $1 AND package_name = '7-Zip'",
               upgraded.with_software)
    got = events(db, upgraded.with_software)
    assert [(e["action"], e["subject"], e["old_value"], e["new_value"]) for e in got] == [
        ("changed", "Chrome", "129.0", "130.0"),
        ("added", "Wireshark", None, "4.2"),
        ("removed", "7-Zip", "23.01", None),
    ]

    # Never had software: its first snapshot is a baseline, not 40 installs.
    for i in range(40):
        add_software(db, upgraded.no_software, f"Package {i}", "1.0")
    assert events(db, upgraded.no_software) == []
    assert len(events(db)) == start + 3


# ------------------------------------------- (4) first-snapshot gate and capture

def test_first_snapshot_gate_then_added_changed_removed(pg_db):
    sid = add_server(pg_db, "gate-srv")

    # Before the first accepted snapshot: nothing is recorded, for any operation.
    add_software(pg_db, sid, "Alpha", "1.0")
    add_software(pg_db, sid, "Beta", "2.0")
    add_software(pg_db, sid, "Gamma", "3.0")
    pg_db.execute("UPDATE server_software_inventory SET version = '1.1' WHERE server_id = $1 AND package_name = 'Alpha'", sid)
    pg_db.execute("DELETE FROM server_software_inventory WHERE server_id = $1 AND package_name = 'Gamma'", sid)
    assert events(pg_db, sid) == []

    baseline(pg_db, sid)
    assert events(pg_db, sid) == []

    add_software(pg_db, sid, "Delta", "4.0", vendor="Delta Corp", architecture="x64", source="hklm",
                 name_key="delta-corp-delta")
    pg_db.execute("UPDATE server_software_inventory SET version = '1.2', name_key = 'alpha-key' "
                  "WHERE server_id = $1 AND package_name = 'Alpha'", sid)
    pg_db.execute("DELETE FROM server_software_inventory WHERE server_id = $1 AND package_name = 'Beta'", sid)

    added, changed, removed = events(pg_db, sid)
    assert (added["category"], added["action"], added["subject"]) == ("software", "added", "Delta")
    assert (added["old_value"], added["new_value"], added["subject_key"]) == (None, "4.0", "delta-corp-delta")
    assert added["detail"] == {"vendor": "Delta Corp", "architecture": "x64", "source": "hklm"}
    assert (added["asset_kind"], added["asset_name"]) == ("server", "gate-srv")

    assert (changed["action"], changed["subject"], changed["old_value"], changed["new_value"]) == (
        "changed", "Alpha", "1.1", "1.2")
    assert changed["subject_key"] == "alpha-key"

    assert (removed["action"], removed["subject"], removed["old_value"], removed["new_value"]) == (
        "removed", "Beta", "2.0", None)
    assert removed["asset_name"] == "gate-srv"


def test_ingest_style_upserts_only_record_real_changes(pg_db):
    """The agent ingest upserts every package on every upload."""
    sid = add_server(pg_db, "upsert-srv")
    upsert = ("INSERT INTO server_software_inventory (server_id, package_name, version, vendor, updated_at) "
              "VALUES ($1, $2, $3, 'v', now()) ON CONFLICT (server_id, package_name) DO UPDATE SET "
              "version = EXCLUDED.version, vendor = EXCLUDED.vendor, updated_at = now()")
    pg_db.execute(upsert, sid, "App", "1.0")
    baseline(pg_db, sid)
    pg_db.execute(upsert, sid, "App", "1.0")
    pg_db.execute(upsert, sid, "App", "1.0")
    assert events(pg_db, sid) == []
    pg_db.execute(upsert, sid, "App", "2.0")
    pg_db.execute(upsert, sid, "App", "2.0")
    pg_db.execute(upsert, sid, "Other", "9.9")
    assert [(e["action"], e["subject"], e["old_value"], e["new_value"]) for e in events(pg_db, sid)] == [
        ("changed", "App", "1.0", "2.0"), ("added", "Other", None, "9.9")]
    # A missing version is a gap in the collector, not an upgrade: value -> NULL,
    # NULL -> NULL and NULL -> value all write nothing (see the NULL-gap tests below).
    count = len(events(pg_db, sid))
    pg_db.execute(upsert, sid, "App", None)
    pg_db.execute(upsert, sid, "App", None)
    pg_db.execute(upsert, sid, "App", "2.0")
    assert len(events(pg_db, sid)) == count


UPSERT_SOFTWARE = (
    "INSERT INTO server_software_inventory (server_id, package_name, version, vendor, updated_at) "
    "VALUES ($1, $2, $3, 'v', now()) ON CONFLICT (server_id, package_name) DO UPDATE SET "
    "version = EXCLUDED.version, vendor = EXCLUDED.vendor, updated_at = now()")


def test_a_missing_version_is_a_gap_not_a_change(pg_db):
    sid = add_server(pg_db, "gap-srv")
    pg_db.execute(UPSERT_SOFTWARE, sid, "Agentx", "4.2")
    baseline(pg_db, sid)

    # X -> NULL then NULL -> X (a partly written uninstall key for one snapshot): nothing.
    pg_db.execute(UPSERT_SOFTWARE, sid, "Agentx", None)
    pg_db.execute(UPSERT_SOFTWARE, sid, "Agentx", "4.2")
    assert events(pg_db, sid) == []

    # X -> Y is still exactly one event.
    pg_db.execute(UPSERT_SOFTWARE, sid, "Agentx", "4.3")
    assert [(e["action"], e["subject"], e["old_value"], e["new_value"]) for e in events(pg_db, sid)] == [
        ("changed", "Agentx", "4.2", "4.3")]

    # A package that appears without a version is still an install; the version
    # arriving later (or going away again) is not an upgrade.
    pg_db.execute(UPSERT_SOFTWARE, sid, "Bare", None)
    pg_db.execute(UPSERT_SOFTWARE, sid, "Bare", "1.0")
    pg_db.execute(UPSERT_SOFTWARE, sid, "Bare", None)
    pg_db.execute(UPSERT_SOFTWARE, sid, "Bare", None)
    assert [(e["action"], e["subject"], e["old_value"], e["new_value"]) for e in events(pg_db, sid)] == [
        ("changed", "Agentx", "4.2", "4.3"), ("added", "Bare", None, None)]


def test_gap_updates_never_enter_plpgsql_and_real_upgrades_do(pg_db):
    sid = add_server(pg_db, "gap-wal-srv")
    for i in range(20):
        pg_db.execute(UPSERT_SOFTWARE, sid, f"pkg-{i}", "1.0")
    baseline(pg_db, sid)
    function = "inventory_software_capture"
    where = f"WHERE server_id = '{sid}'"
    assert plpgsql_calls(pg_db, function, f"UPDATE server_software_inventory SET version = NULL {where}") == 0
    assert plpgsql_calls(pg_db, function, f"UPDATE server_software_inventory SET version = '1.0' {where}") == 0
    assert plpgsql_calls(pg_db, function, f"UPDATE server_software_inventory SET version = '1.1' {where}") == 20
    assert len(events(pg_db, sid)) == 20


def test_software_events_carry_snapshot_truncated_only_while_the_flag_is_set(pg_db):
    sid = add_server(pg_db, "capped-srv")
    for name in ("Keep", "Upgrade", "Drop"):
        pg_db.execute(UPSERT_SOFTWARE, sid, name, "1.0")
    baseline(pg_db, sid)

    def written_since(count: int) -> list[dict]:
        return events(pg_db, sid)[count:]

    # A complete snapshot: the key is absent, not false.
    before = len(events(pg_db, sid))
    pg_db.execute(UPSERT_SOFTWARE, sid, "Fresh", "1.0")
    pg_db.execute(UPSERT_SOFTWARE, sid, "Upgrade", "1.1")
    pg_db.execute("DELETE FROM server_software_inventory WHERE server_id = $1 AND package_name = 'Drop'", sid)
    complete = written_since(before)
    assert [(e["action"], e["subject"]) for e in complete] == [
        ("added", "Fresh"), ("changed", "Upgrade"), ("removed", "Drop")]
    assert all("snapshot_truncated" not in e["detail"] for e in complete), complete
    assert complete[0]["detail"] == {"vendor": "v"}

    # The ingest stamps the flag first, in the same transaction as the rows: all
    # three branches then carry it, next to the detail they already had.
    before = len(events(pg_db, sid))
    pg_db.psql(f"""
        BEGIN;
        UPDATE servers SET software_truncated = true WHERE id = '{sid}';
        INSERT INTO server_software_inventory (server_id, package_name, version, vendor)
             VALUES ('{sid}', 'Cap added', '2.0', 'v');
        UPDATE server_software_inventory SET version = '2.0' WHERE server_id = '{sid}' AND package_name = 'Upgrade';
        DELETE FROM server_software_inventory WHERE server_id = '{sid}' AND package_name = 'Keep';
        COMMIT;
    """)
    capped = written_since(before)
    assert [(e["action"], e["subject"]) for e in capped] == [
        ("added", "Cap added"), ("changed", "Upgrade"), ("removed", "Keep")]
    assert [e["detail"] for e in capped] == [{"vendor": "v", "snapshot_truncated": True}] * 3

    # A later complete snapshot clears it again; events already written keep what they had.
    pg_db.execute("UPDATE servers SET software_truncated = false WHERE id = $1", sid)
    before = len(events(pg_db, sid))
    pg_db.execute(UPSERT_SOFTWARE, sid, "Fresh", "1.5")
    (after,) = written_since(before)
    assert "snapshot_truncated" not in after["detail"]
    assert [e["detail"].get("snapshot_truncated") for e in events(pg_db, sid)] == [None] * 3 + [True] * 3 + [None]


def test_unkeyed_software_rows_are_found_through_the_partial_index(pg_db):
    sid = add_server(pg_db, "idx-srv")
    pg_db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, norm_rev) "
                  "SELECT $1, 'keyed-' || g, '1.0', 1 FROM generate_series(1, 30000) g", sid)
    pg_db.execute("INSERT INTO server_software_inventory (server_id, package_name, version) "
                  "SELECT $1, 'raw-' || g, '1.0' FROM generate_series(1, 5) g", sid)
    pg_db.psql("ANALYZE server_software_inventory")
    query = ("SELECT server_id, package_name FROM server_software_inventory "
             "WHERE norm_rev IS NULL ORDER BY server_id, package_name LIMIT 100")

    plan = "\n".join(row[0] for row in pg_db.query("EXPLAIN " + query))
    assert "idx_ssi_unkeyed" in plan, plan
    assert "Seq Scan" not in plan and "Sort" not in plan, plan
    assert [r[1] for r in pg_db.query(query)] == [f"raw-{g}" for g in range(1, 6)]
    assert pg_db.scalar("SELECT pg_relation_size('idx_ssi_unkeyed')") <= 65536, "a partial index over 5 rows"

    # Keyed rows are not in it: stamping norm_rev empties the index again.
    pg_db.execute("UPDATE server_software_inventory SET norm_rev = 1 WHERE norm_rev IS NULL")
    assert pg_db.query(query) == []


# ------------------------------------------- (5) deleting an asset

def test_deleting_a_server_writes_no_removed_events_and_keeps_history(pg_db):
    sid = add_server(pg_db, "doomed")
    for i in range(25):
        add_software(pg_db, sid, f"Pkg {i}", "1.0")
    baseline(pg_db, sid)
    add_software(pg_db, sid, "Late Install", "3.0")
    assert is_dirty(pg_db, "server", sid)
    before = events(pg_db, sid)
    assert len(before) == 1

    pg_db.execute("DELETE FROM servers WHERE id = $1", sid)

    assert pg_db.scalar("SELECT count(*) FROM server_software_inventory WHERE server_id = $1", sid) == 0
    assert identity(pg_db, "server", sid) is None
    assert events(pg_db, sid) == before, "cascade-deleted software must not read as 25 removals"
    assert [e["action"] for e in events(pg_db)] == ["added"]


def test_pruning_one_package_on_a_surviving_server_is_a_removal(pg_db):
    sid = add_server(pg_db, "survivor")
    add_software(pg_db, sid, "Keep", "1.0")
    add_software(pg_db, sid, "Drop", "1.0")
    baseline(pg_db, sid)
    pg_db.execute("DELETE FROM server_software_inventory WHERE server_id = $1 "
                  "AND updated_at < now() + interval '1 minute' AND package_name <> 'Keep'", sid)
    assert [(e["action"], e["subject"]) for e in events(pg_db, sid)] == [("removed", "Drop")]


def test_deleting_a_device_removes_its_identity_row_but_keeps_its_events(pg_db):
    did = add_device(pg_db, "doomed-fw")
    pg_db.execute("UPDATE inventory_identity SET os_version = '7.2.5', os_product = 'FortiOS' "
                  "WHERE asset_kind = 'device' AND asset_id = $1", did)
    pg_db.execute("UPDATE inventory_identity SET os_version = '7.2.8' "
                  "WHERE asset_kind = 'device' AND asset_id = $1", did)
    assert len(events(pg_db, did)) == 1
    pg_db.execute("INSERT INTO device_template_values (device_id, group_key, metric_key, series_key, value_text) "
                  "VALUES ($1, 'sys', 'fgt_fw_version', 'tpl_fgt_fw_version', '7.2.8')", did)

    pg_db.execute("DELETE FROM devices WHERE id = $1", did)

    assert identity(pg_db, "device", did) is None
    kept = events(pg_db, did)
    assert [(e["category"], e["action"], e["asset_name"]) for e in kept] == [("firmware", "changed", "doomed-fw")]


# --------------------------------------------- (6) suppression

def test_suppression_silences_capture_but_not_dirty_marking(pg_db):
    sid = add_server(pg_db, "quiet-srv")
    add_software(pg_db, sid, "Existing", "1.0")
    add_software(pg_db, sid, "Doomed", "1.0")
    baseline(pg_db, sid)
    did = add_device(pg_db, "quiet-dev", os_version="7.2.5")
    pg_db.execute("UPDATE inventory_identity SET os_version = '7.2.5', hw_serial = 'S1', hw_model = 'M1' "
                  "WHERE asset_kind = 'device' AND asset_id = $1", did)
    mark_clean(pg_db, "device", did)
    mark_clean(pg_db, "server", sid)

    pg_db.execute(f"""
        BEGIN;
        SET LOCAL zenplus.inventory_suppress = 'on';
        INSERT INTO server_software_inventory (server_id, package_name, version) VALUES ('{sid}', 'Quiet Add', '1');
        UPDATE server_software_inventory SET version = '2.0' WHERE server_id = '{sid}' AND package_name = 'Existing';
        DELETE FROM server_software_inventory WHERE server_id = '{sid}' AND package_name = 'Doomed';
        UPDATE inventory_identity SET os_version = '7.2.8', hw_serial = 'S2', hw_model = 'M2'
         WHERE asset_kind = 'device' AND asset_id = '{did}';
        UPDATE devices SET os_version = 'v7.2.8' WHERE id = '{did}';
        UPDATE servers SET os_name = 'Renamed OS' WHERE id = '{sid}';
        COMMIT;
    """)

    assert events(pg_db) == [], "suppressed operations must write no history"
    assert pg_db.scalar("SELECT count(*) FROM server_software_inventory WHERE server_id = $1", sid) == 2
    assert is_dirty(pg_db, "device", did), "dirty-marking is not suppressed (devices)"
    assert is_dirty(pg_db, "server", sid), "dirty-marking is not suppressed (servers)"

    # SET LOCAL ends with its transaction: the same operations record again.
    pg_db.execute("UPDATE server_software_inventory SET version = '3.0' WHERE server_id = $1 AND package_name = 'Existing'", sid)
    pg_db.execute("UPDATE inventory_identity SET os_version = '7.2.9' WHERE asset_kind = 'device' AND asset_id = $1", did)
    assert [(e["category"], e["action"]) for e in events(pg_db)] == [("software", "changed"), ("firmware", "changed")]


# ----------------------------------------------- (7) dirty-marking on devices/servers

def test_device_identity_columns_mark_dirty_only_on_a_real_change(pg_db):
    did = add_device(pg_db, "dirty-dev")
    assert is_dirty(pg_db, "device", did), "AFTER INSERT seeds a dirty row"
    seeded_at = identity(pg_db, "device", did)["source_changed_at"]

    for column in DEVICE_IDENTITY_COLUMNS:
        mark_clean(pg_db, "device", did)
        # same value (including NULL -> NULL): never dirty
        pg_db.execute(f"UPDATE devices SET {column} = {column}, updated_at = now() WHERE id = $1", did)
        assert not is_dirty(pg_db, "device", did), f"{column}: no-op update marked the row dirty"
        pg_db.execute(f"UPDATE devices SET {column} = $2 WHERE id = $1", did, f"value-of-{column}")
        assert is_dirty(pg_db, "device", did), f"{column}: change did not mark the row dirty"
        mark_clean(pg_db, "device", did)
        pg_db.execute(f"UPDATE devices SET {column} = $2 WHERE id = $1", did, f"value-of-{column}")
        assert not is_dirty(pg_db, "device", did), f"{column}: rewriting the same value marked dirty"
    assert identity(pg_db, "device", did)["source_changed_at"] > seeded_at

    # Columns that are not identity never matter.
    mark_clean(pg_db, "device", did)
    pg_db.execute("UPDATE devices SET status = 'up', last_seen = now(), last_rtt_ms = 1.5, description = 'x' WHERE id = $1", did)
    assert not is_dirty(pg_db, "device", did)

    # A change advances source_changed_at, even on a row that is already dirty.
    pg_db.execute("UPDATE devices SET os_version = 'one' WHERE id = $1", did)
    first = identity(pg_db, "device", did)["source_changed_at"]
    time.sleep(0.01)
    pg_db.execute("UPDATE devices SET os_version = 'two' WHERE id = $1", did)
    assert identity(pg_db, "device", did)["source_changed_at"] > first


def test_server_identity_columns_mark_dirty_only_on_a_real_change(pg_db):
    sid = add_server(pg_db, "dirty-srv")
    assert is_dirty(pg_db, "server", sid)

    for column in SERVER_IDENTITY_COLUMNS:
        mark_clean(pg_db, "server", sid)
        pg_db.execute(f"UPDATE servers SET {column} = {column}, updated_at = now() WHERE id = $1", sid)
        assert not is_dirty(pg_db, "server", sid), f"{column}: no-op update marked the row dirty"
        pg_db.execute(f"UPDATE servers SET {column} = $2 WHERE id = $1", sid, f"value-of-{column}")
        assert is_dirty(pg_db, "server", sid), f"{column}: change did not mark the row dirty"
        mark_clean(pg_db, "server", sid)
        pg_db.execute(f"UPDATE servers SET {column} = $2 WHERE id = $1", sid, f"value-of-{column}")
        assert not is_dirty(pg_db, "server", sid)

    # What the agent ingest actually writes on every upload.
    mark_clean(pg_db, "server", sid)
    pg_db.execute("UPDATE servers SET os_name = COALESCE($2, os_name), os_version = COALESCE($3, os_version), "
                  "kernel_or_build = COALESCE($4, kernel_or_build), architecture = COALESCE($5, architecture), "
                  "fqdn = COALESCE($6, fqdn), updated_at = now() WHERE id = $1",
                  sid, "value-of-os_name", "value-of-os_version", None, "value-of-architecture", "h.example")
    assert not is_dirty(pg_db, "server", sid)
    pg_db.execute("UPDATE servers SET status = 'healthy', last_seen = now(), software_truncated = true, "
                  "inventory_updated_at = now(), inventory_collected_at = now(), "
                  "inventory_baselined_at = now(), bios_version = 'B1' WHERE id = $1", sid)
    assert not is_dirty(pg_db, "server", sid)


# --------------------------------------- (8) device_template_values

def _template_row(db, did, key, value, instance=""):
    db.execute("INSERT INTO device_template_values (device_id, group_key, metric_key, instance, series_key, value_text) "
               "VALUES ($1, 'sys', $2, $3, $4, $5) ON CONFLICT (device_id, group_key, metric_key, instance) "
               "DO UPDATE SET value_text = EXCLUDED.value_text, updated_at = now()",
               did, key, instance, f"tpl_{key}", value)


def test_template_identity_metrics_mark_the_owning_device_dirty(pg_db):
    did = add_device(pg_db, "fgt")
    other = add_device(pg_db, "bystander")
    for key in TEMPLATE_IDENTITY_KEYS:
        mark_clean(pg_db, "device", did)
        mark_clean(pg_db, "device", other)
        _template_row(pg_db, did, key, "7.2.5")  # INSERT path
        assert is_dirty(pg_db, "device", did), f"{key}: first value did not mark the device dirty"
        assert not is_dirty(pg_db, "device", other)

        mark_clean(pg_db, "device", did)
        _template_row(pg_db, did, key, "7.2.5")  # same value: no-op upsert
        assert not is_dirty(pg_db, "device", did), f"{key}: unchanged value marked the device dirty"

        _template_row(pg_db, did, key, "7.2.8")  # change
        assert is_dirty(pg_db, "device", did), f"{key}: change did not mark the device dirty"
        assert not is_dirty(pg_db, "device", other)

    # Any other metric, new or changing, leaves the device alone.
    mark_clean(pg_db, "device", did)
    for key in ("fgt_cpu", "fgt_sessions", "fgt_sw_version", "pan_ha_state", "f5_cpu"):
        _template_row(pg_db, did, key, "1")
        _template_row(pg_db, did, key, "2")
    pg_db.execute("UPDATE device_template_values SET value_text = 'z', unit = 'u' WHERE device_id = $1 "
                  "AND metric_key = 'fgt_cpu'", did)
    assert not is_dirty(pg_db, "device", did)
    assert events(pg_db) == [], "dirty-marking alone must never write history"


def _normaliser_template_keys() -> set[str]:
    """Every template metric the normaliser reads: the family keys, the serial keys,
    and any key a family declares. (The normaliser exposes no public constant for
    this, so it is taken from the pack data it loaded.)"""
    from app.services import inventory_normalize  # imported here so a break there fails only the key tests

    net = inventory_normalize._Net
    keys = set(net.template_family) | set(net.template_serial_keys)
    for family in net.families.values():
        keys |= set(family.template_keys)
    return keys


def test_trigger_metric_keys_match_the_template_keys_the_normaliser_reads(pg_db_module):
    """The two device_template_values triggers hard-code their metric keys because a
    WHEN clause must stay a constant. A rule pack that starts reading another
    template-backed metric therefore needs a forward migration widening them; this
    fails until it has one."""
    from app.services import inventory_service

    read = _normaliser_template_keys()
    assert read, "the loaded rule pack declares no template keys, so this test would prove nothing"
    assert set(inventory_service.TEMPLATE_METRIC_KEYS) == read, (
        "inventory_service.TEMPLATE_METRIC_KEYS must list exactly the template metrics the normaliser reads")
    assert set(TEMPLATE_IDENTITY_KEYS) == read

    for trigger in ("inventory_dtv_dirty_ins", "inventory_dtv_dirty_upd"):
        definition = pg_db_module.scalar(
            "SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgname = $1", trigger)
        in_trigger = set(re.findall(r"'([^']*)'", definition))  # the metric keys are its only string literals
        assert in_trigger == read, (
            f"{trigger} lists {sorted(in_trigger)} but the normaliser reads {sorted(read)}: "
            f"widen the trigger with a forward migration (157 is append-only once shipped)")


def test_every_template_metric_the_normaliser_reads_marks_the_device_dirty(pg_db):
    did = add_device(pg_db, "derived-keys")
    for key in sorted(_normaliser_template_keys()):
        mark_clean(pg_db, "device", did)
        _template_row(pg_db, did, key, "1.2.3")
        assert is_dirty(pg_db, "device", did), f"{key}: the normaliser reads it but the trigger ignores it"
        mark_clean(pg_db, "device", did)
        _template_row(pg_db, did, key, "1.2.4")
        assert is_dirty(pg_db, "device", did), f"{key}: a change did not mark the device dirty"


def test_template_value_writes_for_other_metrics_never_enter_plpgsql(pg_db):
    did = add_device(pg_db, "busy-fgt")
    for i in range(300):
        _template_row(pg_db, did, f"metric_{i}", "1", instance=str(i))
    calls = plpgsql_calls(
        pg_db, "inventory_identity_mark_dirty_from_template",
        f"UPDATE device_template_values SET value_text = 'changed', value_num = 1, updated_at = now() WHERE device_id = '{did}'")
    assert calls == 0
    calls = plpgsql_calls(
        pg_db, "inventory_identity_mark_dirty_from_template",
        f"INSERT INTO device_template_values (device_id, group_key, metric_key, instance, series_key, value_text) "
        f"SELECT '{did}', 'sys', 'metric_' || g, 'n' || g, 'tpl_m', 'v' FROM generate_series(1, 300) g")
    assert calls == 0
    # ...while an identity key does, once per changed row.
    assert plpgsql_calls(
        pg_db, "inventory_identity_mark_dirty_from_template",
        f"INSERT INTO device_template_values (device_id, group_key, metric_key, series_key, value_text) "
        f"VALUES ('{did}', 'sys', 'f5_version', 'tpl_f5_version', '17.1.1')") == 1


def test_unchanged_device_writes_never_enter_plpgsql(pg_db):
    ids = [add_device(pg_db, f"poll-{i}", vendor="Fortinet", model="FG", os_version="7.2.5") for i in range(50)]
    assert len(ids) == 50
    for statement in (
        "UPDATE devices SET status = 'up', last_seen = now(), last_rtt_ms = 2.0 WHERE vendor = 'Fortinet'",
        "UPDATE devices SET vendor = vendor, model = model, os_version = os_version, firmware_version = firmware_version, "
        "serial_number = serial_number, sys_object_id = sys_object_id, sys_descr = sys_descr WHERE vendor = 'Fortinet'",
    ):
        assert plpgsql_calls(pg_db, "inventory_identity_mark_dirty", statement) == 0
    assert plpgsql_calls(pg_db, "inventory_identity_mark_dirty",
                         "UPDATE devices SET os_version = '7.2.8' WHERE vendor = 'Fortinet'") == 50


# ----------------------------------------------- (9) identity change capture

def test_identity_os_version_change_writes_one_firmware_event(pg_db):
    did = add_device(pg_db, "edge-fgt")
    where = "WHERE asset_kind = 'device' AND asset_id = $1"

    # NULL -> value is discovery.
    pg_db.execute("UPDATE inventory_identity SET os_version = '7.2.5', os_product = 'FortiOS', os_family = 'fortios', "
                  f"os_release = '7.2', os_source = 'template', norm_rev = 1 {where}", did)
    assert events(pg_db, did) == []

    pg_db.execute(f"UPDATE inventory_identity SET os_version = '7.2.8' {where}", did)
    (event,) = events(pg_db, did)
    assert (event["category"], event["action"], event["asset_kind"]) == ("firmware", "changed", "device")
    assert (event["subject"], event["old_value"], event["new_value"]) == ("FortiOS", "7.2.5", "7.2.8")
    assert event["asset_name"] == "edge-fgt"
    assert event["subject_key"] is None
    assert event["detail"] == {"os_family": "fortios", "os_release": "7.2", "os_source": "template"}

    # The same value again, and a re-normalise that touches other columns: nothing.
    pg_db.execute(f"UPDATE inventory_identity SET os_version = '7.2.8', norm_rev = 2, vendor_key = 'fortinet' {where}", did)
    # value -> NULL is a collector gap, and coming back is not a change either.
    pg_db.execute(f"UPDATE inventory_identity SET os_version = NULL {where}", did)
    pg_db.execute(f"UPDATE inventory_identity SET os_version = '7.2.8' {where}", did)
    assert len(events(pg_db, did)) == 1

    # Raw-evidence writes mark the row dirty but are not history by themselves.
    pg_db.execute("UPDATE devices SET os_version = 'v7.4.0' WHERE id = $1", did)
    assert is_dirty(pg_db, "device", did)
    assert len(events(pg_db, did)) == 1

    # Without a product the subject falls back to the column name.
    pg_db.execute(f"UPDATE inventory_identity SET os_product = NULL, os_version = '7.4.0' {where}", did)
    assert events(pg_db, did)[-1]["subject"] == "os_version"
    assert (events(pg_db, did)[-1]["old_value"], events(pg_db, did)[-1]["new_value"]) == ("7.2.8", "7.4.0")


def test_identity_hardware_changes_write_hardware_events(pg_db):
    did = add_device(pg_db, "chassis")
    where = "WHERE asset_kind = 'device' AND asset_id = $1"

    pg_db.execute(f"UPDATE inventory_identity SET hw_serial = 'FGT-0001', hw_model = 'FG-100F', hw_vendor = 'Fortinet' {where}", did)
    assert events(pg_db, did) == []

    pg_db.execute(f"UPDATE inventory_identity SET hw_serial = 'FGT-0002' {where}", did)
    pg_db.execute(f"UPDATE inventory_identity SET hw_model = 'FG-200F' {where}", did)
    serial, model = events(pg_db, did)
    assert (serial["category"], serial["action"], serial["subject"], serial["old_value"], serial["new_value"]) == (
        "hardware", "changed", "hw_serial", "FGT-0001", "FGT-0002")
    assert (model["category"], model["action"], model["subject"], model["old_value"], model["new_value"]) == (
        "hardware", "changed", "hw_model", "FG-100F", "FG-200F")
    assert serial["detail"]["hw_vendor"] == "Fortinet"

    # Several columns in one statement give one event each.
    pg_db.execute(f"UPDATE inventory_identity SET os_version = '1.0' {where}", did)  # discovery
    assert len(events(pg_db, did)) == 2
    pg_db.execute(f"UPDATE inventory_identity SET hw_serial = 'FGT-0004', hw_model = 'FG-400F', os_version = '2.0' {where}", did)
    batch = events(pg_db, did)[2:]
    assert sorted((e["category"], e["subject"]) for e in batch) == [
        ("firmware", "os_version"), ("hardware", "hw_model"), ("hardware", "hw_serial")]

    # value -> NULL and NULL -> value write nothing.
    count = len(events(pg_db, did))
    pg_db.execute(f"UPDATE inventory_identity SET hw_serial = NULL, hw_model = NULL {where}", did)
    pg_db.execute(f"UPDATE inventory_identity SET hw_serial = 'FGT-9999', hw_model = 'FG-900F' {where}", did)
    assert len(events(pg_db, did)) == count


def test_server_os_change_is_an_os_event(pg_db):
    sid = add_server(pg_db, "win-01")
    where = "WHERE asset_kind = 'server' AND asset_id = $1"
    pg_db.execute("UPDATE inventory_identity SET os_product = 'Windows Server', os_version = '10.0.17763.6532' "
                  f"{where}", sid)
    pg_db.execute(f"UPDATE inventory_identity SET os_version = '10.0.20348.2700' {where}", sid)
    (event,) = events(pg_db, sid)
    assert (event["category"], event["action"], event["asset_kind"], event["asset_name"]) == (
        "os", "changed", "server", "win-01")
    assert (event["subject"], event["old_value"], event["new_value"]) == (
        "Windows Server", "10.0.17763.6532", "10.0.20348.2700")


def test_identity_capture_respects_suppression(pg_db):
    did = add_device(pg_db, "renormalised")
    pg_db.execute("UPDATE inventory_identity SET os_version = '1.0', hw_serial = 'A' "
                  "WHERE asset_kind = 'device' AND asset_id = $1", did)
    pg_db.execute(f"""
        BEGIN;
        SET LOCAL zenplus.inventory_suppress = 'on';
        UPDATE inventory_identity SET os_version = '1.1', hw_serial = 'B'
         WHERE asset_kind = 'device' AND asset_id = '{did}';
        COMMIT;
    """)
    assert events(pg_db) == []


# ------------------- (9b) rule-pack revisions are never history, genuine changes are

@pytest.mark.parametrize("writer", sorted(IDENTITY_WRITERS))
@pytest.mark.parametrize("state", ["clean", "dirty"])
def test_a_rule_pack_revision_bump_writes_no_history(pg_db, writer, state):
    """The pack renders the same source data differently from revision 2 on. Whichever
    writer stores that, and whether the row was clean or dirty, it is not a change."""
    write = IDENTITY_WRITERS[writer]
    sid = add_server(pg_db, f"bump-{writer}-{state}")
    write(pg_db, sid, 1, "10.0.20348.2700", "SN-0001", "PowerEdge R740")
    assert events(pg_db, sid) == []
    assert revisions(pg_db, "server", sid) == (1, 1)

    if state == "dirty":
        # Any source change marks the row dirty: norm_rev forgets, values_rev must not.
        pg_db.execute("UPDATE servers SET cpu_model = 'Xeon Gold' WHERE id = $1", sid)
        assert is_dirty(pg_db, "server", sid)
        assert revisions(pg_db, "server", sid) == (None, 1)

    write(pg_db, sid, 2, "10.0.20348", "SN0001", "R740")  # os_version, hw_serial and hw_model re-rendered
    assert events(pg_db, sid) == [], "a rule-pack revision change was recorded as history"
    assert revisions(pg_db, "server", sid) == (2, 2)
    assert identity(pg_db, "server", sid)["os_version"] == "10.0.20348"

    # Rolling the release back re-renders the other way: still nothing.
    write(pg_db, sid, 1, "10.0.20348.2700", "SN-0001", "PowerEdge R740")
    write(pg_db, sid, 2, "10.0.20348", "SN0001", "R740")
    assert events(pg_db, sid) == []

    # The new revision is the baseline: a genuine change at revision 2 is one event per value.
    write(pg_db, sid, 2, "10.0.26100", "SN0002", "R750")
    assert sorted((e["category"], e["subject"], e["old_value"], e["new_value"]) for e in events(pg_db, sid)) == [
        ("hardware", "hw_model", "R740", "R750"),
        ("hardware", "hw_serial", "SN0001", "SN0002"),
        ("os", "Windows Server", "10.0.20348", "10.0.26100"),
    ]


@pytest.mark.parametrize("writer", sorted(IDENTITY_WRITERS))
def test_a_genuine_change_at_the_same_revision_writes_exactly_one_event(pg_db, writer):
    write = IDENTITY_WRITERS[writer]
    sid = add_server(pg_db, f"genuine-{writer}")
    write(pg_db, sid, 1, "10.0.17763.6532", "SN-1", "Model A")

    # The source really changed: dirty, then stored at the same revision.
    pg_db.execute("UPDATE servers SET kernel_or_build = '10.0.20348.2700' WHERE id = $1", sid)
    assert is_dirty(pg_db, "server", sid)
    write(pg_db, sid, 1, "10.0.20348.2700", "SN-1", "Model A")
    (event,) = events(pg_db, sid)
    assert (event["category"], event["action"], event["subject"]) == ("os", "changed", "Windows Server")
    assert (event["old_value"], event["new_value"]) == ("10.0.17763.6532", "10.0.20348.2700")

    # Storing the same values again, at the same revision, is not a second event.
    write(pg_db, sid, 1, "10.0.20348.2700", "SN-1", "Model A")
    assert len(events(pg_db, sid)) == 1


def test_the_first_write_that_records_a_revision_is_never_history(pg_db):
    """A row whose values predate values_rev (it is NULL) has no pack to compare with."""
    sid = add_server(pg_db, "no-revision-yet")
    pg_db.execute("UPDATE inventory_identity SET os_version = '1.0', hw_model = 'A' "
                  "WHERE asset_kind = 'server' AND asset_id = $1", sid)
    assert revisions(pg_db, "server", sid) == (None, None)
    write_identity_like_the_loop(pg_db, sid, 1, "2.0", None, "B")
    assert events(pg_db, sid) == []
    assert revisions(pg_db, "server", sid) == (1, 1)


def test_dirty_marking_never_touches_values_rev(pg_db):
    did = add_device(pg_db, "rev-dev")
    pg_db.execute("UPDATE inventory_identity SET os_version = '7.2.5', norm_rev = 4, values_rev = 4 "
                  "WHERE asset_kind = 'device' AND asset_id = $1", did)
    pg_db.execute("UPDATE devices SET os_version = 'v7.2.8' WHERE id = $1", did)
    assert revisions(pg_db, "device", did) == (None, 4)
    pg_db.execute("INSERT INTO device_template_values (device_id, group_key, metric_key, series_key, value_text) "
                  "VALUES ($1, 'sys', 'fgt_fw_version', 'tpl_fgt_fw_version', '7.2.8')", did)
    assert revisions(pg_db, "device", did) == (None, 4)


# ------------------------------------- 10,000 no-change updates, and the app role

def test_ten_thousand_no_change_upserts_emit_nothing_and_skip_plpgsql(pg_db):
    sid = add_server(pg_db, "bulk-srv")
    pg_db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, vendor) "
                  "SELECT $1, 'pkg-' || g, '1.' || g, 'v' FROM generate_series(1, 10000) g", sid)
    baseline(pg_db, sid)
    upsert = (f"INSERT INTO server_software_inventory (server_id, package_name, version, vendor, updated_at) "
              f"SELECT '{sid}', 'pkg-' || g, {{version}}, 'v', now() FROM generate_series(1, 10000) g "
              f"ON CONFLICT (server_id, package_name) DO UPDATE SET version = EXCLUDED.version, "
              f"vendor = EXCLUDED.vendor, updated_at = now()")
    same = upsert.format(version="'1.' || g")

    start = time.perf_counter()
    pg_db.psql(same)
    with_triggers = time.perf_counter() - start

    assert pg_db.scalar("SELECT count(*) FROM inventory_change_events") == 0
    assert plpgsql_calls(pg_db, "inventory_software_capture", same) == 0

    pg_db.execute("ALTER TABLE server_software_inventory DISABLE TRIGGER inventory_ssi_changed")
    start = time.perf_counter()
    pg_db.psql(same)
    without_trigger = time.perf_counter() - start
    pg_db.execute("ALTER TABLE server_software_inventory ENABLE TRIGGER inventory_ssi_changed")
    print(f"\n10,000 no-change upserts: {with_triggers * 1000:.0f} ms with the capture trigger, "
          f"{without_trigger * 1000:.0f} ms with it disabled; events written: 0, PL/pgSQL calls: 0")
    assert with_triggers < 30, "no-change upserts should be nowhere near this slow"

    # Positive control: the counter and the trigger do see real changes.
    changed = upsert.format(version="CASE WHEN g <= 100 THEN '2.' || g ELSE '1.' || g END")
    assert plpgsql_calls(pg_db, "inventory_software_capture", changed) == 100
    assert pg_db.scalar("SELECT count(*) FROM inventory_change_events WHERE action = 'changed'") == 100


def test_capture_triggers_work_under_the_application_role(pg_db):
    """The app connects as zenplus; trigger functions run with its privileges."""
    for table in ("inventory_identity", "inventory_change_events", "inventory_daily_snapshots"):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert pg_db.scalar("SELECT has_table_privilege('zenplus', $1, $2)", table, privilege), (table, privilege)
    assert pg_db.scalar("SELECT has_sequence_privilege('zenplus', 'inventory_change_events_id_seq', 'USAGE')")

    sid = add_server(pg_db, "app-role")
    add_software(pg_db, sid, "App", "1.0")
    baseline(pg_db, sid)
    pg_db.execute("INSERT INTO server_software_inventory (server_id, package_name, version) VALUES ($1, 'New', '1')",
                  sid, user="zenplus")
    pg_db.execute("UPDATE server_software_inventory SET version = '2.0' WHERE server_id = $1 AND package_name = 'App'",
                  sid, user="zenplus")
    pg_db.execute("DELETE FROM server_software_inventory WHERE server_id = $1 AND package_name = 'New'",
                  sid, user="zenplus")
    pg_db.execute("UPDATE servers SET os_name = 'Windows Server 2022' WHERE id = $1", sid, user="zenplus")
    pg_db.execute("UPDATE inventory_identity SET os_version = '1', norm_rev = 1 WHERE asset_kind = 'server' AND asset_id = $1",
                  sid, user="zenplus")
    pg_db.execute("UPDATE inventory_identity SET os_version = '2' WHERE asset_kind = 'server' AND asset_id = $1",
                  sid, user="zenplus")
    assert [(e["category"], e["action"]) for e in events(pg_db, sid)] == [
        ("software", "added"), ("software", "changed"), ("software", "removed"), ("os", "changed")]
    pg_db.execute("DELETE FROM servers WHERE id = $1", sid, user="zenplus")
    assert identity(pg_db, "server", sid) is None


# ---------------------------------------------------------------- (10) lint

def test_lint_migrations_passes_and_locks_154_after_153():
    env = {**os.environ, "ZENPLUS_DIR": str(REPO_ROOT)}  # build-release defaults to /opt/zenplus
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "build-release.py"), "lint-migrations"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr

    lock = [line.split(None, 1) for line in (SCRIPTS_DIR / "migrations.lock").read_text().splitlines() if line.strip()]
    names = [name for _, name in lock]
    assert names.count(MIGRATION) == 1
    assert names.index(MIGRATION) > names.index("migrate-153-force-default-admin-password-change.sql")
    digest = dict((name, digest) for digest, name in lock)[MIGRATION]
    assert digest == hashlib.sha256(MIGRATION_PATH.read_bytes()).hexdigest()


# ------------------------------------------------- the fixture's own safety rails

def test_fixture_refuses_the_appliance_port(tmp_path):
    with pytest.raises(RuntimeError, match="5432"):
        pg_fixture.PgCluster(Path("/nonexistent"), Path("/nonexistent/psql"), tmp_path, 5432)
    assert all(pg_fixture._free_port() != 5432 for _ in range(50))


def _provision_outcome() -> str:
    """What provisioning does when PostgreSQL cannot be found: 'skip', 'error: <why>'
    or 'provisioned'. Asserting on the outcome (not pytest.raises) means a wrong one
    fails the test instead of being reported as a skip."""
    try:
        pg_fixture._provision()
    except pytest.skip.Exception:
        return "skip"
    except RuntimeError as exc:
        return f"error: {exc}"
    return "provisioned"


def test_missing_postgres_binaries_skip_unless_required(monkeypatch):
    monkeypatch.setattr(pg_fixture, "_find_pg_bin", lambda: None)
    monkeypatch.delenv("ZENPLUS_REQUIRE_PG", raising=False)
    monkeypatch.delenv("CI", raising=False)
    assert _provision_outcome() == "skip"
    monkeypatch.setenv("ZENPLUS_REQUIRE_PG", "1")
    assert _provision_outcome().startswith("error: private PostgreSQL required but unavailable")


@pytest.mark.parametrize("ci", ["true", "TRUE", "1"])
def test_ci_makes_a_missing_postgres_a_failure_without_any_opt_in(monkeypatch, ci):
    """GitHub Actions sets CI=true; the workflow need not also set ZENPLUS_REQUIRE_PG."""
    monkeypatch.setattr(pg_fixture, "_find_pg_bin", lambda: None)
    monkeypatch.delenv("ZENPLUS_REQUIRE_PG", raising=False)
    monkeypatch.setenv("CI", ci)
    assert _provision_outcome().startswith("error: private PostgreSQL required but unavailable")
    monkeypatch.setenv("CI", "false")
    assert _provision_outcome() == "skip"


def test_children_never_inherit_libpq_environment(monkeypatch):
    monkeypatch.setenv("PGHOST", "203.0.113.9")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("PGDATABASE", "zenplus")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://fixture@127.0.0.1:1/fixture")
    env = pg_fixture._clean_env()
    assert not [k for k in env if k.startswith("PG") and k != "PGCONNECT_TIMEOUT"]


@pytest.mark.parametrize("temp_root", ["short", "too_long_for_a_socket"])
def test_a_failed_provision_stops_the_cluster_and_removes_its_directories(monkeypatch, tmp_path, temp_root):
    if pg_fixture._find_pg_bin() is None or os.geteuid() == 0:
        pg_fixture._unavailable("needs the PostgreSQL binaries and a non-root user")
    if temp_root == "too_long_for_a_socket":
        long_root = tmp_path / ("t" * 90)
        long_root.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(long_root))
    else:
        # pytest's own tmp_path (and any TMPDIR) can already be too long for a socket path, so
        # pin the "short" case to a root that is short wherever the suite runs.
        monkeypatch.setattr(tempfile, "tempdir", "/tmp")
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(**kwargs):
        made.append(real_mkdtemp(**kwargs))
        return made[-1]

    started: list = []
    real_start = pg_fixture.PgCluster._start

    def recording_start(self):
        real_start(self)
        started.append(self)

    def exploding_replay(self, *args, **kwargs):
        raise RuntimeError("replay exploded")

    monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)
    monkeypatch.setattr(pg_fixture.PgCluster, "_start", recording_start)
    monkeypatch.setattr(pg_fixture.PgCluster, "replay_migrations", exploding_replay)

    with pytest.raises(RuntimeError, match="replay exploded"):
        pg_fixture._provision()

    # The cluster root, plus a socket directory under /tmp when the root is too long for a socket path.
    assert len(made) == (1 if temp_root == "short" else 2), made
    assert not [m for m in made if Path(m).exists()], "every temp directory must be deleted"
    assert len(started) == 1 and started[0].proc.poll() is not None, "the postmaster must be stopped"
    assert not started[0].sock_dir.exists()
    if temp_root != "short":
        assert started[0].sock_dir.parent == Path("/tmp")


# ------------------------------------- the private cluster is not reachable from outside

def test_the_cluster_listens_only_on_a_private_unix_socket(pg_cluster):
    """Trust auth for a superuser is only acceptable when nothing else can connect."""
    port, sock_dir = pg_cluster.port, pg_cluster.sock_dir
    assert stat.S_IMODE(os.stat(sock_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(pg_cluster.root).st_mode) == 0o700
    assert os.stat(sock_dir).st_uid == os.getuid()
    assert stat.S_ISSOCK(os.stat(sock_dir / f".s.PGSQL.{port}").st_mode)
    assert len(str(sock_dir / f".s.PGSQL.{port}")) < pg_fixture.SOCKET_PATH_LIMIT

    assert pg_cluster.run_psql("postgres", "SHOW listen_addresses").stdout.strip() == ""
    assert pg_cluster.run_psql("postgres", "SHOW unix_socket_directories").stdout.strip() == str(sock_dir)
    assert pg_cluster.run_psql("postgres", "SHOW unix_socket_permissions").stdout.strip() == "0700"
    # No TCP listener on the port, and were there one, pg_hba would turn every host connection away.
    with pytest.raises(OSError):
        socket.create_connection((pg_fixture.HOST, port), timeout=3).close()
    refused = subprocess.run(
        [str(pg_cluster._psql), "-X", "-w", "-h", pg_fixture.HOST, "-p", str(port), "-U", pg_fixture.SUPERUSER,
         "-d", "postgres", "-c", "SELECT 1"],
        capture_output=True, text=True, env=pg_fixture._clean_env(), timeout=30)
    assert refused.returncode != 0
    assert pg_cluster.run_psql(
        "postgres", "SELECT count(*) FROM pg_hba_file_rules WHERE type LIKE 'host%' AND auth_method <> 'reject'"
    ).stdout.strip() == "0"


def test_connection_urls_work_through_sqlalchemy_and_asyncpg(pg_db):
    assert pg_db.url.startswith("postgresql+asyncpg://zen@/") and "host=" in pg_db.url and "port=" in pg_db.url
    assert "127.0.0.1" not in pg_db.url

    async def go():
        seen = {}
        for user in ("zen", "zenplus"):
            url = pg_db.url if user == "zen" else pg_db.url_for(user)
            engine = create_async_engine(url, poolclass=NullPool)
            try:
                async with engine.connect() as conn:
                    row = (await conn.execute(text("SELECT current_user, current_database()"))).one()
                seen[user] = tuple(row)
            finally:
                await engine.dispose()
        conn = await asyncpg.connect(pg_db.url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            seen["asyncpg"] = await conn.fetchval("SELECT current_user")
        finally:
            await conn.close()
        return seen

    assert asyncio.run(go()) == {
        "zen": ("zen", pg_db.name), "zenplus": ("zenplus", pg_db.name), "asyncpg": "zen"}
    assert pg_db.scalar("SELECT current_user", user="zenplus") == "zenplus"
    assert pg_db.psql("SELECT 41 + 1").strip() == "42"


def test_the_socket_directory_is_private_and_short_enough(tmp_path):
    # Built under /tmp on purpose: a TMPDIR of any length must not turn "short" into "long".
    short = Path(tempfile.mkdtemp(prefix="zp-sockdir-", dir="/tmp"))
    long_root = tmp_path / ("r" * 120)
    long_root.mkdir()
    made: list[Path] = []
    try:
        inside = pg_fixture._make_socket_dir(short)
        made.append(inside)
        assert inside == short / "sock", "a short root keeps its socket inside it"

        outside = pg_fixture._make_socket_dir(long_root)
        made.append(outside)
        assert outside.parent == Path("/tmp") and long_root not in outside.parents
        for directory in (inside, outside):
            assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
            assert len(f"{directory}/.s.PGSQL.65535") <= pg_fixture.SOCKET_PATH_LIMIT
    finally:
        shutil.rmtree(short, ignore_errors=True)
        shutil.rmtree(made[-1], ignore_errors=True)


def test_children_get_the_server_bin_directory_first_on_path(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("PGHOST", "203.0.113.9")
    env = pg_fixture._clean_env(Path("/opt/pgsql/bin"))
    assert env["PATH"].split(os.pathsep) == ["/opt/pgsql/bin", "/usr/local/bin", "/usr/bin"]
    assert not [k for k in env if k.startswith("PG") and k != "PGCONNECT_TIMEOUT"]
    assert pg_fixture._clean_env()["PATH"] == "/usr/local/bin:/usr/bin"


def test_the_migration_runner_finds_psql_even_when_it_is_not_on_path(pg_cluster, monkeypatch, tmp_path):
    """run-migrations.py runs a bare ``psql``; a server-only install keeps it in the bin directory."""
    if not (pg_cluster.bin_dir / "psql").exists():
        pytest.skip("this install keeps psql outside the server bin directory, so the case does not arise")
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    assert shutil.which("psql") is None
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "migrations.lock").write_text("")
    db = pg_cluster.create_database(template=None)
    try:
        output = pg_cluster.replay_migrations(db.name, scripts_dir=scripts, lock=scripts / "migrations.lock",
                                              include_init=False)
        assert "apply" not in output
        assert db.scalar("SELECT to_regclass('schema_migrations')") == "schema_migrations"
    finally:
        db.drop()
