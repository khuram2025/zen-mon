"""SQL-level tests for migrate-161-device-status-confirmation.sql.

Confirmed device status persists its state machine in ``device_status_state``,
flap history and notice state in ``device_flap_episodes``, and mirrors the flag
on ``devices`` (docs/device-status-policy.md). The upgrade tests build a
database from every migration except 161, fill it the way a running appliance
would be (devices in every status, open alerts), upgrade it through the real
runner and apply 161 a second time. They check the schema, the defaults
existing rows get, the runner's evidence (baseline) detection and the zenplus
grants. The constraint tests use a fresh clone of the fully migrated template.
Private cluster only (``pg_fixture``), never the appliance database.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncpg
import pytest

from pg_fixture import (  # noqa: F401  (fixtures are used by name)
    SCRIPTS_DIR,
    PgDatabase,
    copy_migration_set,
    pg_cluster,
    pg_db,
)

M161 = "migrate-161-device-status-confirmation.sql"
M161_PATH = SCRIPTS_DIR / M161
UTC = timezone.utc

STATUSES = ("up", "degraded", "down", "unknown", "maintenance")
NEW_TABLES = ("device_status_state", "device_flap_episodes")
TOUCHED_TABLES = ("devices", "alert_rules", *NEW_TABLES)
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")

# What the runner probes to decide that 161 already ran (tables, added columns, named indexes).
EVIDENCE_TABLES = {"device_status_state", "device_flap_episodes"}
EVIDENCE_COLUMNS = {"devices.is_flapping", "devices.flapping_since", "devices.flap_kind",
                    "devices.status_changed_at", "alert_rules.notify_while_flapping"}
EVIDENCE_INDEXES = {"idx_devices_is_flapping", "idx_device_flap_episodes_device_started",
                    "uq_device_flap_episodes_open", "idx_device_flap_episodes_pending_notice"}


def _load_runner():
    """scripts/run-migrations.py (hyphenated, so not importable by name); shared with test_migration_runner."""
    module = sys.modules.get("run_migrations")
    if module is None:
        spec = importlib.util.spec_from_file_location("run_migrations", SCRIPTS_DIR / "run-migrations.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules["run_migrations"] = module  # dataclasses resolve annotations through sys.modules
        spec.loader.exec_module(module)
    return module


runner = _load_runner()


# ------------------------------------------------------------------ helpers

def present_objects(db: PgDatabase) -> set[str]:
    """The object set the runner's evidence probe sees, read through the fixture's psql (private socket, clean env)."""
    original = runner.run_psql
    runner.run_psql = lambda cmd, sql=None, file=None: db.run_psql(sql, file=file, check=False)
    try:
        return runner.existing_objects([])
    finally:
        runner.run_psql = original


def migration_161():
    return runner.Migration(path=M161_PATH, filename=M161,
                            checksum=hashlib.sha256(M161_PATH.read_bytes()).hexdigest())


_seq = itertools.count(1)


def add_device(db: PgDatabase, hostname: str, status: str = "unknown", **cols) -> uuid.UUID:
    n = next(_seq)
    names = ["hostname", "ip_address", "status", *cols]
    marks = ", ".join(f"${i + 1}" + ("::inet" if name == "ip_address" else "") for i, name in enumerate(names))
    ip = f"10.161.{n // 250}.{n % 250 + 1}"
    return db.scalar(f"INSERT INTO devices ({', '.join(names)}) VALUES ({marks}) RETURNING id",
                     hostname, ip, status, *cols.values())


def schema_fingerprint(db: PgDatabase) -> list[tuple]:
    """Columns, indexes, constraints, storage options and grants of every table 161 touches."""
    return db.query(
        """
        SELECT 'column', c.table_name || '.' || c.column_name,
               concat_ws(' ', c.data_type, c.udt_name, c.character_maximum_length, c.is_nullable, c.column_default)
          FROM information_schema.columns c
         WHERE c.table_schema = 'public' AND c.table_name = ANY($1::text[])
        UNION ALL
        SELECT 'index', i.indexname, i.indexdef
          FROM pg_indexes i
         WHERE i.schemaname = 'public' AND i.tablename = ANY($1::text[])
        UNION ALL
        SELECT 'constraint', con.conrelid::regclass::text || '.' || con.conname, pg_get_constraintdef(con.oid)
          FROM pg_constraint con
         WHERE con.conrelid = ANY(SELECT ('public.' || t)::regclass FROM unnest($1::text[]) t)
        UNION ALL
        SELECT 'relation', c.relname,
               concat_ws(' ', array_to_string(c.reloptions, ','), c.relacl::text, obj_description(c.oid, 'pg_class'))
          FROM pg_class c
         WHERE c.relnamespace = 'public'::regnamespace AND c.relname = ANY($1::text[])
        ORDER BY 1, 2, 3
        """,
        list(TOUCHED_TABLES))


def columns_of(db: PgDatabase, table: str) -> dict[str, tuple]:
    """column -> (data_type or array udt, varchar length, nullable)."""
    rows = db.query(
        "SELECT column_name, CASE WHEN data_type = 'ARRAY' THEN udt_name ELSE data_type END, "
        "       character_maximum_length, is_nullable = 'YES' "
        "  FROM information_schema.columns WHERE table_schema = 'public' AND table_name = $1", table)
    return {name: (kind, length, nullable) for name, kind, length, nullable in rows}


def indexdef(db: PgDatabase, name: str) -> str | None:
    return db.scalar("SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = $1", name)


# ------------------------------------------------------------------ N-1 -> N upgrade

@pytest.fixture(scope="module")
def upgraded(pg_cluster, tmp_path_factory):
    """An appliance at 160 with devices in every status and open alerts -> 161 via the runner -> 161 again."""
    scripts, lock = copy_migration_set(tmp_path_factory.mktemp("m161_n_minus_1"), exclude=[M161])
    db = pg_cluster.create_database(f"zp_m161_{uuid.uuid4().hex[:8]}", template=None)
    try:
        pg_cluster.replay_migrations(db.name, scripts_dir=scripts, lock=lock)
        assert db.scalar("SELECT to_regclass('device_status_state')") is None
        assert db.scalar("SELECT count(*) FROM information_schema.columns "
                         "WHERE table_name = 'devices' AND column_name = 'is_flapping'") == 0
        present_before = present_objects(db)

        seen = datetime.now(UTC) - timedelta(minutes=3)
        devices = {status: add_device(db, f"m161-{status}", status, last_seen=seen, last_rtt_ms=12.5)
                   for status in STATUSES}
        rule = db.scalar("INSERT INTO alert_rules (name, metric, operator, threshold, trigger_on) "
                         "VALUES ('m161 reachability', 'ping_status', 'eq', 0, 'any') RETURNING id")
        alerts = {
            "down": db.scalar("INSERT INTO alerts (rule_id, device_id, status, severity, message) "
                              "VALUES ($1, $2, 'active', 'critical', 'm161-down is down') RETURNING id",
                              rule, devices["down"]),
            "degraded": db.scalar("INSERT INTO alerts (rule_id, device_id, status, severity, message, metadata) "
                                  "VALUES ($1, $2, 'pending', 'warning', 'm161-degraded is degraded', $3) "
                                  "RETURNING id", rule, devices["degraded"], {"hold_seconds": 120}),
        }
        rows_before = snapshot(db)

        output = pg_cluster.replay_migrations(db.name)   # the real upgrade path
        schema_after_first = schema_fingerprint(db)
        db.psql_file(M161_PATH)                            # applied a second time
        yield SimpleNamespace(db=db, devices=devices, rule=rule, alerts=alerts, output=output,
                              rows_before=rows_before, schema_after_first=schema_after_first,
                              present_before=present_before)
    finally:
        db.drop()


def snapshot(db: PgDatabase) -> dict:
    return {
        "devices": db.query("SELECT id, status, last_seen, last_rtt_ms, updated_at FROM devices "
                            "WHERE hostname LIKE 'm161-%' ORDER BY hostname"),
        "alerts": db.query("SELECT id, rule_id, device_id, status, severity, message, metadata, resolved_at "
                           "FROM alerts ORDER BY message"),
        "rules": db.query("SELECT id, name, enabled, trigger_on FROM alert_rules WHERE name LIKE 'm161 %'"),
    }


def test_the_runner_applies_161_and_records_its_checksum(upgraded, pg_cluster):
    assert f"apply   {M161}" in upgraded.output
    assert upgraded.db.scalar("SELECT checksum FROM schema_migrations WHERE filename = $1", M161) == \
        migration_161().checksum
    again = pg_cluster.replay_migrations(upgraded.db.name)
    assert f"skip    {M161}" in again
    assert not [line for line in again.splitlines() if line.startswith(("apply", "baseline"))]


def test_applying_161_twice_changes_nothing(upgraded):
    assert schema_fingerprint(upgraded.db) == upgraded.schema_after_first
    assert snapshot(upgraded.db) == upgraded.rows_before


def test_existing_devices_keep_their_status_and_get_the_defaults(upgraded):
    """Seeded silently by the writers later: the migration rewrites no device row and opens nothing."""
    db, ids = upgraded.db, list(upgraded.devices.values())
    rows = db.query("SELECT status, is_flapping, flapping_since, flap_kind, status_changed_at "
                    "FROM devices WHERE id = ANY($1::uuid[]) ORDER BY hostname", ids)
    assert sorted(r[0] for r in rows) == sorted(STATUSES)
    assert all(r[1:] == (False, None, None, None) for r in rows), rows
    assert snapshot(db)["devices"] == upgraded.rows_before["devices"], "no UPDATE touched the rows (updated_at kept)"
    for table in NEW_TABLES:
        assert db.scalar(f"SELECT count(*) FROM {table} WHERE device_id = ANY($1::uuid[])", ids) == 0


def test_open_alerts_and_rules_survive_and_rules_do_not_notify_while_flapping(upgraded):
    db = upgraded.db
    assert snapshot(db)["alerts"] == upgraded.rows_before["alerts"]
    assert db.query("SELECT status FROM alerts WHERE id = ANY($1::uuid[]) ORDER BY status",
                    list(upgraded.alerts.values())) == [("active",), ("pending",)]
    assert db.scalar("SELECT notify_while_flapping FROM alert_rules WHERE id = $1", upgraded.rule) is False
    assert db.scalar("SELECT count(*) FROM alert_rules WHERE notify_while_flapping IS NOT FALSE") == 0


def test_devices_and_alert_rules_gain_the_mirrored_columns(upgraded):
    db = upgraded.db
    devices = columns_of(db, "devices")
    assert devices["is_flapping"] == ("boolean", None, False)
    assert devices["flapping_since"] == ("timestamp with time zone", None, True)
    assert devices["flap_kind"] == ("character varying", 16, True)
    assert devices["status_changed_at"] == ("timestamp with time zone", None, True)
    assert columns_of(db, "alert_rules")["notify_while_flapping"] == ("boolean", None, False)
    defaults = dict(db.query(
        "SELECT table_name || '.' || column_name, column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name IN ('is_flapping', 'notify_while_flapping') "
        "AND table_name IN ('devices', 'alert_rules')"))
    assert defaults == {"devices.is_flapping": "false", "alert_rules.notify_while_flapping": "false"}


def test_device_status_state_has_the_contracted_shape(upgraded):
    assert columns_of(upgraded.db, "device_status_state") == {
        "device_id": ("uuid", None, False),
        "owner": ("text", None, False),
        "hard_status": ("character varying", 20, False),
        "in_maintenance": ("boolean", None, False),
        "status_since": ("timestamp with time zone", None, True),
        "fail_streak": ("smallint", None, False),
        "ok_streak": ("smallint", None, False),
        "clear_streak": ("smallint", None, False),
        "window_bits": ("integer", None, False),
        "window_len": ("smallint", None, False),
        "reach_changes": ("_timestamptz", None, False),
        "perf_changes": ("_timestamptz", None, False),
        "reach_flapping": ("boolean", None, False),
        "perf_flapping": ("boolean", None, False),
        "flapping_since": ("timestamp with time zone", None, True),
        "last_sample_at": ("timestamp with time zone", None, True),
        "updated_at": ("timestamp with time zone", None, False),
    }
    db = upgraded.db
    assert db.scalar("SELECT reloptions FROM pg_class WHERE oid = 'device_status_state'::regclass") == \
        ["fillfactor=80"], "updated on most poll cycles: page space keeps updates HOT"
    assert db.scalar("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                     "WHERE conrelid = 'device_status_state'::regclass AND contype = 'p'") == "PRIMARY KEY (device_id)"
    check = db.scalar("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                      "WHERE conname = 'device_status_state_hard_status_check'")
    assert check is not None
    for value in ("up", "degraded", "down", "unknown"):
        assert f"'{value}'" in check
    assert "maintenance" not in check and "flapping" not in check


def test_device_flap_episodes_has_the_contracted_shape(upgraded):
    assert columns_of(upgraded.db, "device_flap_episodes") == {
        "id": ("uuid", None, False),
        "device_id": ("uuid", None, False),
        "started_at": ("timestamp with time zone", None, False),
        "ended_at": ("timestamp with time zone", None, True),
        "kind": ("character varying", 16, False),
        "start_changes": ("integer", None, True),
        "window_minutes": ("integer", None, True),
        "status_at_start": ("character varying", 20, True),
        "status_at_end": ("character varying", 20, True),
        "end_reason": ("character varying", 20, True),
        "start_notice": ("character varying", 12, False),
        "stop_notice": ("character varying", 12, False),
        "notice_channels": ("_uuid", None, False),
        "created_at": ("timestamp with time zone", None, False),
    }
    assert "gen_random_uuid()" in upgraded.db.scalar(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'device_flap_episodes' AND column_name = 'id'")


def test_named_indexes_exist_with_their_predicates(upgraded):
    db = upgraded.db
    flapping = indexdef(db, "idx_devices_is_flapping")
    assert flapping is not None and "ON public.devices" in flapping and flapping.endswith("WHERE is_flapping")
    history = indexdef(db, "idx_device_flap_episodes_device_started")
    assert history is not None and "(device_id, started_at DESC)" in history
    open_episode = indexdef(db, "uq_device_flap_episodes_open")
    assert open_episode is not None and open_episode.startswith("CREATE UNIQUE INDEX")
    assert "(device_id) WHERE (ended_at IS NULL)" in open_episode
    pending = indexdef(db, "idx_device_flap_episodes_pending_notice")
    assert pending is not None and "(started_at)" in pending
    assert "start_notice" in pending and "stop_notice" in pending and "'pending'" in pending


def test_both_tables_cascade_from_devices(upgraded):
    rows = upgraded.db.query(
        "SELECT conrelid::regclass::text, confdeltype::text FROM pg_constraint "
        "WHERE contype = 'f' AND confrelid = 'devices'::regclass AND conrelid = ANY($1::regclass[]) ORDER BY 1",
        list(NEW_TABLES))
    assert rows == [("device_flap_episodes", "c"), ("device_status_state", "c")]


def test_zenplus_has_dml_on_the_new_tables(upgraded):
    db = upgraded.db
    for table in NEW_TABLES:
        for privilege in DML:
            assert db.scalar("SELECT has_table_privilege('zenplus', $1, $2)", table, privilege), (table, privilege)


def test_the_runner_recognises_161_from_its_objects(upgraded, pg_cluster):
    """A database that has 161's objects but no ledger row is baselined, never re-run or reported unresolved."""
    order = runner.migration_order
    sql = M161_PATH.read_text()
    assert {name.lower() for name in order.created_objects(sql)} == EVIDENCE_TABLES
    assert order.added_columns(sql) == EVIDENCE_COLUMNS
    assert order.created_indexes(sql) == EVIDENCE_INDEXES
    assert not order.writes_rows(sql), "no row inserts: replay is never refused as unverifiable"
    assert order.is_replay_safe(sql)

    migration, db = migration_161(), upgraded.db
    assert not runner.evidence_of(migration, upgraded.present_before)
    present = present_objects(db)
    assert runner.evidence_of(migration, present)
    probes = EVIDENCE_TABLES | {f"column:{c}" for c in EVIDENCE_COLUMNS} | {f"index:{i}" for i in EVIDENCE_INDEXES}
    for probe in sorted(probes):
        assert not runner.evidence_of(migration, present - {probe}), f"{probe} is not part of the evidence"

    checksum = db.scalar("SELECT checksum FROM schema_migrations WHERE filename = $1", M161)
    db.execute("DELETE FROM schema_migrations WHERE filename = $1", M161)
    output = pg_cluster.replay_migrations(db.name)
    assert f"baseline {M161} (objects already present)" in output
    assert db.scalar("SELECT checksum FROM schema_migrations WHERE filename = $1", M161) == checksum


def test_161_is_one_transaction_and_guards_its_grants():
    """A failed run must leave no evidence behind (the runner would baseline it and the grants would never
    run), and an install without the zenplus role must still migrate."""
    sql = M161_PATH.read_text()
    statements = [s.strip().upper() for s in runner.migration_order.analyzable(sql).split(";") if s.strip()]
    assert statements[0] == "BEGIN" and statements[-1] == "COMMIT"
    assert statements.count("BEGIN") == 1 and statements.count("COMMIT") == 1
    guard = sql.index("IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zenplus') THEN")
    assert guard < sql.index("GRANT ") and sql.index("END IF;") > sql.rindex("GRANT ")


def test_a_fresh_install_matches_the_upgrade(upgraded, pg_db):
    assert schema_fingerprint(pg_db) == schema_fingerprint(upgraded.db)


# ------------------------------------------------------------------ the writers' statements, as zenplus

# Shapes of the central persist (one statement per device per check) and of the
# sensor batch write, reduced to what exercises the new tables and their grants.
CENTRAL_PERSIST = """
WITH owner AS (
    SELECT device_id FROM device_polling_owner WHERE device_id = $1 AND owner_kind = 'central'
), dev AS (
    UPDATE devices d
       SET status = $2, last_seen = $3, last_rtt_ms = 41.5,
           is_flapping = $4, flapping_since = $5, flap_kind = $6,
           status_changed_at = COALESCE($7, d.status_changed_at)
      FROM owner o
     WHERE d.id = o.device_id
    RETURNING d.id
), st AS (
    INSERT INTO device_status_state AS s
           (device_id, owner, hard_status, status_since, ok_streak, window_bits, window_len,
            reach_changes, reach_flapping, flapping_since, last_sample_at, updated_at)
    SELECT id, 'central', $2, $7, 1, 0, 1, $8::timestamptz[], $4, $5, $3, NOW() FROM dev
    ON CONFLICT (device_id) DO UPDATE
       SET owner = EXCLUDED.owner, hard_status = EXCLUDED.hard_status,
           status_since = COALESCE(EXCLUDED.status_since, s.status_since),
           ok_streak = EXCLUDED.ok_streak, window_bits = EXCLUDED.window_bits,
           window_len = EXCLUDED.window_len, reach_changes = EXCLUDED.reach_changes,
           reach_flapping = EXCLUDED.reach_flapping, flapping_since = EXCLUDED.flapping_since,
           last_sample_at = EXCLUDED.last_sample_at, updated_at = EXCLUDED.updated_at
    RETURNING s.device_id, s.hard_status, s.reach_flapping, s.flapping_since
)
INSERT INTO device_flap_episodes (device_id, started_at, kind, start_changes, window_minutes, status_at_start)
SELECT device_id, flapping_since, 'reachability', 4, 30, hard_status FROM st WHERE reach_flapping
ON CONFLICT (device_id) WHERE ended_at IS NULL DO NOTHING
"""

SENSOR_TAKEOVER = """
WITH batch AS (
    SELECT * FROM jsonb_to_recordset($1::jsonb)
        AS b(device_id uuid, status text, sample_at timestamptz)
), dev AS (
    UPDATE devices d
       SET status = b.status, is_flapping = false, flapping_since = NULL, flap_kind = NULL,
           status_changed_at = b.sample_at
      FROM batch b
     WHERE d.id = b.device_id
    RETURNING d.id
), st AS (
    INSERT INTO device_status_state AS s (device_id, owner, hard_status, status_since, last_sample_at)
    SELECT b.device_id, $2, b.status, b.sample_at, b.sample_at FROM batch b JOIN dev ON dev.id = b.device_id
    ON CONFLICT (device_id) DO UPDATE
       SET owner = EXCLUDED.owner, hard_status = EXCLUDED.hard_status, status_since = EXCLUDED.status_since,
           fail_streak = 0, ok_streak = 0, clear_streak = 0, window_bits = 0, window_len = 0,
           reach_changes = '{}', perf_changes = '{}', reach_flapping = false, perf_flapping = false,
           flapping_since = NULL, last_sample_at = EXCLUDED.last_sample_at, updated_at = NOW()
    RETURNING s.device_id
)
UPDATE device_flap_episodes e
   SET ended_at = NOW(), end_reason = 'owner_change', status_at_end = 'up'
  FROM st
 WHERE e.device_id = st.device_id AND e.ended_at IS NULL
"""


def test_central_and_sensor_writes_run_as_zenplus(pg_db):
    # install.sh grants zenplus every existing table (schema-wide default privileges); the private
    # cluster does not run it, so grant the one pre-existing table these statements write the same way.
    pg_db.execute("GRANT SELECT, UPDATE ON devices TO zenplus")
    device = add_device(pg_db, "m161-writer", "up")
    now = datetime.now(UTC)
    changes = [now - timedelta(minutes=m) for m in (9, 6, 3, 1)]

    for _ in range(2):   # a repeated persist converges: one state row, one open episode
        pg_db.execute(CENTRAL_PERSIST, device, "down", now, True, changes[-1], "reachability", now, changes,
                      user="zenplus")
    assert pg_db.query("SELECT owner, hard_status, reach_flapping, cardinality(reach_changes) "
                       "FROM device_status_state WHERE device_id = $1", device) == [("central", "down", True, 4)]
    assert pg_db.query("SELECT status, is_flapping, flap_kind FROM devices WHERE id = $1", device) == \
        [("down", True, "reachability")]
    assert pg_db.query("SELECT kind, start_notice, stop_notice, notice_channels, ended_at FROM device_flap_episodes "
                       "WHERE device_id = $1", device) == [("reachability", "pending", "pending", [], None)]

    sensor = str(uuid.uuid4())
    batch = [{"device_id": str(device), "status": "up", "sample_at": now.isoformat()}]
    pg_db.execute(SENSOR_TAKEOVER, batch, sensor, user="zenplus")
    assert pg_db.query("SELECT owner, hard_status, reach_flapping, reach_changes FROM device_status_state "
                       "WHERE device_id = $1", device) == [(sensor, "up", False, [])]
    assert pg_db.query("SELECT end_reason, status_at_end, ended_at IS NOT NULL FROM device_flap_episodes "
                       "WHERE device_id = $1", device) == [("owner_change", "up", True)]
    assert pg_db.scalar("SELECT is_flapping FROM devices WHERE id = $1", device) is False

    # sensor offline: its state rows go, the device is reseeded by whoever polls it next
    assert pg_db.execute("DELETE FROM device_status_state WHERE owner = $1", sensor, user="zenplus") == "DELETE 1"
    assert pg_db.execute("DELETE FROM device_flap_episodes WHERE device_id = $1", device, user="zenplus") == "DELETE 1"


# ------------------------------------------------------------------ constraints the writers rely on

def test_hard_status_is_never_maintenance_or_flapping(pg_db):
    device = add_device(pg_db, "m161-check")
    for bad in ("maintenance", "flapping"):
        with pytest.raises(asyncpg.CheckViolationError):
            pg_db.execute("INSERT INTO device_status_state (device_id, owner, hard_status) VALUES ($1, 'central', $2)",
                          device, bad)
    pg_db.execute("INSERT INTO device_status_state (device_id, owner) VALUES ($1, 'central')", device)
    for good in ("up", "degraded", "down", "unknown"):
        pg_db.execute("UPDATE device_status_state SET hard_status = $2 WHERE device_id = $1", device, good)
    for bad in ("maintenance", "flapping"):
        with pytest.raises(asyncpg.CheckViolationError):
            pg_db.execute("UPDATE device_status_state SET hard_status = $2 WHERE device_id = $1", device, bad)
    assert pg_db.scalar("SELECT hard_status FROM device_status_state WHERE device_id = $1", device) == "unknown"


def test_a_new_state_row_starts_from_the_contracted_defaults(pg_db):
    device = add_device(pg_db, "m161-defaults")
    pg_db.execute("INSERT INTO device_status_state (device_id, owner) VALUES ($1, 'central')", device)
    row = pg_db.query(
        "SELECT hard_status, in_maintenance, status_since, fail_streak, ok_streak, clear_streak, window_bits, "
        "       window_len, reach_changes, perf_changes, reach_flapping, perf_flapping, flapping_since, "
        "       last_sample_at, updated_at IS NOT NULL "
        "  FROM device_status_state WHERE device_id = $1", device)
    assert row == [("unknown", False, None, 0, 0, 0, 0, 0, [], [], False, False, None, None, True)]
    with pytest.raises(asyncpg.NotNullViolationError):
        pg_db.execute("INSERT INTO device_status_state (device_id) VALUES ($1)", add_device(pg_db, "m161-no-owner"))


def test_only_one_open_episode_per_device(pg_db):
    device, other = add_device(pg_db, "m161-flappy"), add_device(pg_db, "m161-other")
    start = datetime.now(UTC) - timedelta(minutes=20)
    open_episode = ("INSERT INTO device_flap_episodes (device_id, started_at, kind) VALUES ($1, $2, $3) "
                    "ON CONFLICT (device_id) WHERE ended_at IS NULL DO NOTHING RETURNING id")
    first = pg_db.scalar(open_episode, device, start, "reachability")
    assert first is not None
    assert pg_db.scalar(open_episode, device, start + timedelta(minutes=1), "performance") is None
    with pytest.raises(asyncpg.UniqueViolationError):
        pg_db.execute("INSERT INTO device_flap_episodes (device_id, started_at, kind) VALUES ($1, $2, 'both')",
                      device, start)
    assert pg_db.scalar(open_episode, other, start, "performance") is not None, "per device, not global"

    pg_db.execute("UPDATE device_flap_episodes SET ended_at = $2, end_reason = 'stable' WHERE id = $1",
                  first, start + timedelta(minutes=5))
    second = pg_db.scalar(open_episode, device, start + timedelta(minutes=10), "both")
    assert second is not None and second != first
    pg_db.execute("UPDATE device_flap_episodes SET ended_at = now(), end_reason = 'disabled' WHERE id = $1", second)
    assert pg_db.scalar(open_episode, device, start + timedelta(minutes=15), "reachability") is not None
    assert pg_db.query("SELECT count(*), count(*) FILTER (WHERE ended_at IS NULL) FROM device_flap_episodes "
                       "WHERE device_id = $1", device) == [(3, 1)], "closed episodes accumulate as history"


def test_deleting_a_device_removes_its_state_and_episodes(pg_db):
    device, kept = add_device(pg_db, "m161-deleted", "down"), add_device(pg_db, "m161-kept", "up")
    for target in (device, kept):
        pg_db.execute("INSERT INTO device_status_state (device_id, owner, hard_status) VALUES ($1, 'central', 'down')",
                      target)
        pg_db.execute("INSERT INTO device_flap_episodes (device_id, started_at, ended_at, kind) "
                      "VALUES ($1, now() - interval '1 hour', now() - interval '30 minutes', 'both')", target)
        pg_db.execute("INSERT INTO device_flap_episodes (device_id, started_at, kind) VALUES ($1, now(), 'both')",
                      target)
    pg_db.execute("DELETE FROM devices WHERE id = $1", device)
    for table in NEW_TABLES:
        assert pg_db.scalar(f"SELECT count(*) FROM {table} WHERE device_id = $1", device) == 0, table
        assert pg_db.scalar(f"SELECT count(*) FROM {table} WHERE device_id = $1", kept) > 0, table
