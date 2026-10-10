"""SQL-level tests for migrate-158-inventory-phase1-followups.sql (UP-03, UP-06, UP-08 / DT-04, X6).

An appliance that ran Inventory phase 0 had every server 'Never reported', every software title 'first seen' on the day of the
upgrade and no record of when change capture began. 158 repairs those from data the appliance already holds. The tests build a
database from every migration except 157 and 158, fill it the way a running appliance would be, and upgrade it through the
real runner; then they re-apply 158 to prove it is idempotent. Private cluster only (``pg_fixture``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pg_fixture import (  # noqa: F401  (fixtures are used by name)
    SCRIPTS_DIR,
    copy_migration_set,
    pg_cluster,
)

M157 = "migrate-157-inventory-foundation.sql"
M158 = "migrate-158-inventory-phase1-followups.sql"
UTC = timezone.utc


def _snapshot(db):
    return {
        "stamps": db.query("SELECT display_name, inventory_updated_at FROM servers ORDER BY display_name"),
        "first_seen": db.query("SELECT s.display_name, w.package_name, w.first_seen_at FROM server_software_inventory w "
                               "JOIN servers s ON s.id = w.server_id ORDER BY 1, 2"),
        "capture": db.scalar("SELECT value FROM system_settings WHERE key = 'inventory.capture_started_at'"),
    }


@pytest.fixture(scope="module")
def upgraded(pg_cluster, tmp_path_factory):
    """pre-157 data -> 157 -> an agent writes a stamp -> 158 (each step through the real runner)."""
    scripts, lock = copy_migration_set(tmp_path_factory.mktemp("n_minus_2"), exclude=[M157, M158])
    scripts157, lock157 = copy_migration_set(tmp_path_factory.mktemp("n_minus_1"), exclude=[M158])
    db = pg_cluster.create_database(f"zp_m158_{uuid.uuid4().hex[:8]}", template=None)
    try:
        pg_cluster.replay_migrations(db.name, scripts_dir=scripts, lock=lock)
        assert db.scalar("SELECT to_regclass('inventory_identity')") is None
        now = datetime.now(UTC)
        old_update = now - timedelta(days=3)
        ids = SimpleNamespace()
        ids.reporting = db.scalar("INSERT INTO servers (display_name) VALUES ('p-reporting') RETURNING id")
        ids.quiet = db.scalar("INSERT INTO servers (display_name) VALUES ('p-no-software') RETURNING id")
        ids.stamped = db.scalar("INSERT INTO servers (display_name) VALUES ('p-stamped') RETURNING id")
        ids.silent = db.scalar("INSERT INTO servers (display_name) VALUES ('p-silent') RETURNING id")
        rows = [
            # (server, title, install_date, updated_at)
            (ids.reporting, "Chrome", now - timedelta(days=900), now - timedelta(hours=5)),
            (ids.reporting, "7-Zip", now - timedelta(days=40), now - timedelta(hours=2)),             # newest: the stamp
            (ids.reporting, "No Date", None, now - timedelta(hours=9)),
            (ids.reporting, "Epoch Date", datetime(1970, 1, 1, tzinfo=UTC), now - timedelta(hours=9)),   # not plausible
            (ids.reporting, "Future Date", now + timedelta(days=30), now - timedelta(hours=9)),          # not a past date
            (ids.stamped, "Git", now - timedelta(days=100), now - timedelta(days=1)),
            (ids.silent, "Zoom", now - timedelta(days=400), old_update),
        ]
        for server, title, installed, updated in rows:
            db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, install_date, updated_at) "
                       "VALUES ($1, $2, '1.0', $3, $4)", server, title, installed, updated)
        ids.newest = now - timedelta(hours=2)
        ids.silent_at = old_update
        # 157 arms the capture; the agent of 'p-stamped' then uploads, which stamps it (158 must never replace that)
        pg_cluster.replay_migrations(db.name, scripts_dir=scripts157, lock=lock157)
        assert db.scalar("SELECT to_regclass('inventory_identity')") is not None
        assert db.scalar("SELECT to_regclass('idx_ssi_endpoint_protection')") is None
        db.execute("UPDATE servers SET inventory_updated_at = $2 WHERE id = $1", ids.stamped, now - timedelta(minutes=30))
        ids.stamped_at = db.scalar("SELECT inventory_updated_at FROM servers WHERE id = $1", ids.stamped)
        output = pg_cluster.replay_migrations(db.name)
        yield SimpleNamespace(db=db, ids=ids, output=output, now=now, rows=rows)
    finally:
        db.drop()


def test_158_applies_after_157(upgraded):
    assert f"apply   {M158}" in upgraded.output and M157 not in upgraded.output.replace(f"skip    {M157}", "")
    assert upgraded.db.scalar("SELECT applied_at FROM schema_migrations WHERE filename = $1", M158) > \
        upgraded.db.scalar("SELECT applied_at FROM schema_migrations WHERE filename = $1", M157)


def test_the_capture_start_is_when_157_was_applied(upgraded):
    """UP-08 / DT-04: history starts the day the capture triggers were armed, not the day of the first event."""
    db = upgraded.db
    value = db.scalar("SELECT value FROM system_settings WHERE key = 'inventory.capture_started_at'")
    assert isinstance(value, str) and value.endswith("Z") and len(value) == 27       # microseconds: 2026-10-09T11:18:58.246551Z
    applied = db.scalar("SELECT applied_at FROM schema_migrations WHERE filename = $1", M157)
    assert datetime.fromisoformat(value.replace("Z", "+00:00")) == applied


def test_servers_that_reported_get_the_time_we_last_received_their_list(upgraded):
    """UP-03 / DT-03: every server read 'Never' after the upgrade. The honest stamp is the newest software row."""
    db, ids = upgraded.db, upgraded.ids
    stamp = lambda sid: db.scalar("SELECT inventory_updated_at FROM servers WHERE id = $1", sid)    # noqa: E731
    assert abs(stamp(ids.reporting) - ids.newest) < timedelta(seconds=1)
    assert abs(stamp(ids.silent) - ids.silent_at) < timedelta(seconds=1), "a silent server keeps its old, true date"
    assert stamp(ids.stamped) == ids.stamped_at, "a stamp the agent wrote is never replaced"
    assert stamp(ids.quiet) is None, "no software list: nothing was ever received"


def test_software_that_pre_dates_the_capture_gets_its_install_date_as_first_seen(upgraded):
    """UP-06: 157's DEFAULT now() stamped every old row with the migration time."""
    db, now = upgraded.db, upgraded.now
    first = {r[0]: r[1] for r in db.query("SELECT package_name, first_seen_at FROM server_software_inventory")}
    capture = datetime.fromisoformat(db.scalar("SELECT value FROM system_settings WHERE key = 'inventory.capture_started_at'")
                                     .replace("Z", "+00:00"))
    assert abs(first["Chrome"] - (now - timedelta(days=900))) < timedelta(seconds=1)
    assert abs(first["7-Zip"] - (now - timedelta(days=40))) < timedelta(seconds=1)
    assert abs(first["Zoom"] - (now - timedelta(days=400))) < timedelta(seconds=1)
    for title in ("No Date", "Epoch Date", "Future Date"):          # no usable date: the upgrade time stays (a lower bound is unknown)
        assert abs(first[title] - capture) < timedelta(minutes=5), title
        assert first[title] <= capture + timedelta(seconds=1)


def test_the_repair_writes_no_change_events(upgraded):
    assert upgraded.db.scalar("SELECT count(*) FROM inventory_change_events") == 0


def test_reapplying_158_changes_nothing_and_leaves_later_rows_alone(upgraded):
    db, ids = upgraded.db, upgraded.ids
    # a title first seen after the capture began, installed long before: Inventory really did first see it today
    db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, install_date, updated_at) "
               "VALUES ($1, 'Late Arrival', '1.0', $2, now())", ids.reporting, upgraded.now - timedelta(days=700))
    before = _snapshot(db)
    db.execute("UPDATE servers SET inventory_updated_at = NULL WHERE id = $1", ids.quiet)
    db.psql_file(SCRIPTS_DIR / M158)
    after = _snapshot(db)
    assert after == before
    late = db.scalar("SELECT first_seen_at FROM server_software_inventory WHERE package_name = 'Late Arrival'")
    assert late > datetime.fromisoformat(before["capture"].replace("Z", "+00:00")), "written after the capture began: untouched"
    assert db.scalar("SELECT inventory_updated_at FROM servers WHERE id = $1", ids.quiet) is None


def test_the_read_path_indexes_exist(upgraded):
    for name in ("idx_ssi_endpoint_protection", "idx_ssvc_endpoint_protection", "idx_servers_device_id"):
        assert upgraded.db.scalar("SELECT to_regclass($1)", name) is not None, name


def test_a_fresh_replay_records_a_capture_start_and_survives_a_second_run(pg_cluster):
    db = pg_cluster.create_database(f"zp_m158f_{uuid.uuid4().hex[:8]}", template=None)
    try:
        pg_cluster.replay_migrations(db.name)
        first = db.scalar("SELECT value FROM system_settings WHERE key = 'inventory.capture_started_at'")
        assert first is not None
        db.psql_file(SCRIPTS_DIR / M158)
        assert db.scalar("SELECT value FROM system_settings WHERE key = 'inventory.capture_started_at'") == first
        assert db.scalar("SELECT count(*) FROM system_settings WHERE key = 'inventory.capture_started_at'") == 1
    finally:
        db.drop()


def test_without_a_ledger_row_the_time_of_the_migration_is_used(pg_cluster):
    db = pg_cluster.create_database(f"zp_m158n_{uuid.uuid4().hex[:8]}", template=None)
    try:
        pg_cluster.replay_migrations(db.name)
        db.execute("DELETE FROM system_settings WHERE key = 'inventory.capture_started_at'")
        db.execute("DELETE FROM schema_migrations WHERE filename = $1", M157)
        db.psql_file(SCRIPTS_DIR / M158)
        value = db.scalar("SELECT value FROM system_settings WHERE key = 'inventory.capture_started_at'")
        assert abs(datetime.fromisoformat(value.replace("Z", "+00:00")) - datetime.now(UTC)) < timedelta(minutes=2)
    finally:
        db.drop()
