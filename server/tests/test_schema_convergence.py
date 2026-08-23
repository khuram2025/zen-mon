"""Tests for the schema-convergence machinery.

The bug these guard against: an appliance took a v1.6.0 stamp while its
ClickHouse ``zenplus.snmp_metrics`` table did not exist, because the previous
sync baselined a hardcoded list of legacy migrations — recording them as
applied without running them — whenever the ledger was empty. SNMP polling
succeeded and every write failed with "Table ... does not exist".
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(SCRIPTS_DIR))
migration_order = _load("migration_order", "migration_order.py")
ch_migrate = _load("ch_migrate", "ch_migrate.py")


# ─── Ordering ─────────────────────────────────────────────────────────────────


def test_lockfile_line_order_beats_filename_sort(tmp_path):
    """Duplicate sequence numbers make filename sort meaningless."""
    for name in ("migrate-030-b.sql", "migrate-030-a.sql", "migrate-002-x.sql"):
        (tmp_path / name).write_text("SELECT 1;")
    (tmp_path / "migrations.lock").write_text(
        "aaa  migrate-030-b.sql\n"
        "bbb  migrate-002-x.sql\n"
        "ccc  migrate-030-a.sql\n"
    )

    ordered = [p.name for p in migration_order.ordered_migrations(tmp_path)]

    assert ordered == ["migrate-030-b.sql", "migrate-002-x.sql", "migrate-030-a.sql"]


def test_unlocked_migrations_sort_after_locked_ones(tmp_path):
    for name in ("migrate-001-a.sql", "migrate-999-wip.sql"):
        (tmp_path / name).write_text("SELECT 1;")
    (tmp_path / "migrations.lock").write_text("aaa  migrate-001-a.sql\n")

    ordered = [p.name for p in migration_order.ordered_migrations(tmp_path)]

    assert ordered == ["migrate-001-a.sql", "migrate-999-wip.sql"]


def test_engine_split_is_by_filename(tmp_path):
    for name in ("migrate-001-a.sql", "migrate-002-b-clickhouse.sql"):
        (tmp_path / name).write_text("SELECT 1;")

    pg = migration_order.ordered_migrations(tmp_path, engine="postgres")
    ch = migration_order.ordered_migrations(tmp_path, engine="clickhouse")

    assert [p.name for p in pg] == ["migrate-001-a.sql"]
    assert [p.name for p in ch] == ["migrate-002-b-clickhouse.sql"]


def test_write_lock_appends_without_reordering(tmp_path):
    lock = tmp_path / "migrations.lock"
    migration_order.write_lock(lock, [("migrate-030-b.sql", "aaa"), ("migrate-002-x.sql", "bbb")])

    entries = migration_order.load_lock(lock)

    assert entries == [("migrate-030-b.sql", "aaa"), ("migrate-002-x.sql", "bbb")]


# ─── Static analysis ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql, safe",
    [
        ("CREATE TABLE IF NOT EXISTS zenplus.t (a Int) ENGINE = Log;", True),
        ("CREATE TABLE zenplus.t (a Int) ENGINE = Log;", False),
        ("CREATE MATERIALIZED VIEW zenplus.mv TO zenplus.t AS SELECT 1;", False),
        ("CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.mv TO zenplus.t AS SELECT 1;", True),
        ("CREATE TABLE IF NOT EXISTS zenplus.t (a Int);\nINSERT INTO zenplus.t SELECT 1;", False),
        ("ALTER TABLE devices ADD COLUMN IF NOT EXISTS x TEXT;", True),
        ("ALTER TABLE devices ADD COLUMN x TEXT;", False),
        ("-- INSERT INTO nope\nCREATE TABLE IF NOT EXISTS zenplus.t (a Int);", True),
    ],
)
def test_replay_safety_detection(sql, safe):
    assert migration_order.is_replay_safe(sql) is safe


def test_created_objects_covers_tables_and_materialized_views():
    sql = (
        "CREATE TABLE IF NOT EXISTS zenplus.snmp_metrics (a Int);\n"
        "CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.snmp_metrics_5m_mv TO x AS SELECT 1;\n"
    )
    assert migration_order.created_objects(sql) == [
        "zenplus.snmp_metrics",
        "zenplus.snmp_metrics_5m_mv",
    ]


def test_every_shipped_clickhouse_migration_is_verifiable_or_replayable():
    """The invariant the ClickHouse sync depends on.

    For each shipped migration the sync must be able to either prove it ran (it
    creates objects it can probe for) or safely re-run it (guarded DDL, no
    backfill). A migration that is neither can drift undetected, which is
    exactly how snmp_metrics went missing while the ledger claimed otherwise.
    """
    unverifiable = []
    for path in migration_order.ordered_migrations(SCRIPTS_DIR, engine="clickhouse"):
        sql = path.read_text()
        if not migration_order.created_objects(sql) and not migration_order.is_replay_safe(sql):
            unverifiable.append(path.name)
    assert not unverifiable, unverifiable


# ─── ClickHouse convergence ───────────────────────────────────────────────────


class FakeClickHouse:
    """Minimal ClickHouse stand-in that tracks which objects exist."""

    def __init__(self, objects: set[str] | None = None, ledger: dict | None = None):
        self.objects = {o.lower() for o in (objects or set())}
        self.ledger = dict(ledger or {})
        self.executed: list[str] = []

    def __call__(self, sql: str, timeout: int = 300) -> str:
        stripped = sql.strip()
        if stripped.upper().startswith("SELECT NAME FROM SYSTEM.TABLES"):
            return "\n".join(sorted(o.split(".", 1)[1] for o in self.objects)) + "\n"
        if "FROM zenplus.schema_migrations" in stripped:
            return "".join(f"{k}\t{v}\n" for k, v in sorted(self.ledger.items()))
        if stripped.upper().startswith("INSERT INTO ZENPLUS.SCHEMA_MIGRATIONS"):
            name = stripped.split("VALUES ('", 1)[1].split("'", 1)[0]
            self.ledger[name] = "recorded"
            return ""
        if "CREATE DATABASE" in stripped or "schema_migrations" in stripped:
            return ""
        # A real migration body.
        self.executed.append(stripped)
        for obj in migration_order.created_objects(stripped):
            self.objects.add(obj.lower())
        return ""


SNMP_SQL = (
    "CREATE TABLE IF NOT EXISTS zenplus.snmp_metrics (a Int) ENGINE = Log;\n"
    "CREATE TABLE IF NOT EXISTS zenplus.snmp_if_metrics (a Int) ENGINE = Log;\n"
)
ROLLUP_SQL = (
    "CREATE TABLE IF NOT EXISTS zenplus.ping_metrics_5m (a Int) ENGINE = Log;\n"
    "CREATE MATERIALIZED VIEW zenplus.ping_metrics_5m_mv TO zenplus.ping_metrics_5m AS SELECT 1;\n"
    "INSERT INTO zenplus.ping_metrics_5m SELECT 1;\n"
)


def _scripts(tmp_path, files: dict[str, str]) -> Path:
    for name, sql in files.items():
        (tmp_path / name).write_text(sql)
    return tmp_path


def test_heals_a_migration_the_ledger_falsely_claims_was_applied(tmp_path):
    """The exact field failure: ledger says applied, snmp_metrics is missing."""
    scripts = _scripts(tmp_path, {"migrate-004-snmp-clickhouse.sql": SNMP_SQL})
    ch = FakeClickHouse(objects=set(), ledger={"migrate-004-snmp-clickhouse.sql": "stale"})

    summary = ch_migrate.sync(scripts, query=ch)

    assert summary["healed"] == ["migrate-004-snmp-clickhouse.sql"]
    assert "zenplus.snmp_metrics" in ch.objects
    assert "zenplus.snmp_if_metrics" in ch.objects


def test_applies_a_migration_that_was_never_delivered(tmp_path):
    scripts = _scripts(tmp_path, {"migrate-004-snmp-clickhouse.sql": SNMP_SQL})
    ch = FakeClickHouse()

    summary = ch_migrate.sync(scripts, query=ch)

    assert summary["applied"] == ["migrate-004-snmp-clickhouse.sql"]
    assert ch.ledger["migrate-004-snmp-clickhouse.sql"] == "recorded"


def test_baselines_only_when_the_objects_really_exist(tmp_path):
    """A pre-ledger appliance that genuinely ran the migration is recorded, not re-run."""
    scripts = _scripts(tmp_path, {"migrate-004-snmp-clickhouse.sql": SNMP_SQL})
    ch = FakeClickHouse(objects={"zenplus.snmp_metrics", "zenplus.snmp_if_metrics"})

    summary = ch_migrate.sync(scripts, query=ch)

    assert summary["baselined"] == ["migrate-004-snmp-clickhouse.sql"]
    assert ch.executed == []


def test_up_to_date_appliance_does_nothing(tmp_path):
    scripts = _scripts(tmp_path, {"migrate-004-snmp-clickhouse.sql": SNMP_SQL})
    ch = FakeClickHouse(
        objects={"zenplus.snmp_metrics", "zenplus.snmp_if_metrics"},
        ledger={"migrate-004-snmp-clickhouse.sql": "ok"},
    )

    summary = ch_migrate.sync(scripts, query=ch)

    assert ch.executed == []
    assert not any(summary[k] for k in ("applied", "healed", "baselined", "failed"))


def test_non_replay_safe_migration_runs_on_a_clean_database(tmp_path):
    scripts = _scripts(tmp_path, {"migrate-012-ping-rollups-clickhouse.sql": ROLLUP_SQL})
    ch = FakeClickHouse()

    summary = ch_migrate.sync(scripts, query=ch)

    assert summary["applied"] == ["migrate-012-ping-rollups-clickhouse.sql"]


def test_partially_applied_backfill_is_reported_not_replayed(tmp_path):
    """Replaying a blind INSERT would duplicate rows — flag it for a human."""
    scripts = _scripts(tmp_path, {"migrate-012-ping-rollups-clickhouse.sql": ROLLUP_SQL})
    ch = FakeClickHouse(objects={"zenplus.ping_metrics_5m"})

    summary = ch_migrate.sync(scripts, query=ch)

    assert ch.executed == []
    assert len(summary["unresolved"]) == 1
    assert summary["unresolved"][0]["missing"] == ["zenplus.ping_metrics_5m_mv"]


def test_a_failing_migration_is_reported_and_left_unrecorded(tmp_path):
    scripts = _scripts(tmp_path, {"migrate-004-snmp-clickhouse.sql": SNMP_SQL})

    class Failing(FakeClickHouse):
        def __call__(self, sql, timeout=300):
            if "CREATE TABLE IF NOT EXISTS zenplus.snmp_metrics" in sql:
                raise ch_migrate.ClickHouseError("boom")
            return super().__call__(sql, timeout)

    ch = Failing()
    summary = ch_migrate.sync(scripts, query=ch)

    assert summary["failed"][0]["filename"] == "migrate-004-snmp-clickhouse.sql"
    assert "migrate-004-snmp-clickhouse.sql" not in ch.ledger


def test_dry_run_changes_nothing(tmp_path):
    scripts = _scripts(tmp_path, {"migrate-004-snmp-clickhouse.sql": SNMP_SQL})
    ch = FakeClickHouse()

    summary = ch_migrate.sync(scripts, query=ch, dry_run=True)

    assert summary["pending"] == ["migrate-004-snmp-clickhouse.sql"]
    assert ch.executed == []
    assert ch.ledger == {}


def test_migrations_apply_in_release_order(tmp_path):
    scripts = _scripts(tmp_path, {
        "migrate-030-b-clickhouse.sql": "CREATE TABLE IF NOT EXISTS zenplus.b (a Int);",
        "migrate-030-a-clickhouse.sql": "CREATE TABLE IF NOT EXISTS zenplus.a (a Int);",
    })
    (scripts / "migrations.lock").write_text(
        "x  migrate-030-b-clickhouse.sql\ny  migrate-030-a-clickhouse.sql\n"
    )
    ch = FakeClickHouse()

    summary = ch_migrate.sync(scripts, query=ch)

    assert summary["applied"] == ["migrate-030-b-clickhouse.sql", "migrate-030-a-clickhouse.sql"]


# ─── The gate's verdict ───────────────────────────────────────────────────────

sync_schema = _load("sync_schema", "sync-schema.py")


def test_clean_reports_produce_an_ok_verdict():
    status = sync_schema.summarize(
        {"applied": ["migrate-063-x.sql"], "skipped": [], "pending": [], "drift": [], "failed": []},
        {"applied": [], "baselined": [], "healed": [], "pending": [], "failed": [], "unresolved": []},
    )

    assert status["ok"] is True
    assert status["problems"] == []


def test_healing_alone_is_not_drift():
    """A migration the sync repaired is resolved, not a reason to roll back."""
    status = sync_schema.summarize(
        {"applied": [], "skipped": [], "pending": [], "drift": [], "failed": []},
        {"applied": [], "baselined": [], "healed": ["migrate-004-snmp-clickhouse.sql"],
         "pending": [], "failed": [], "unresolved": []},
    )

    assert status["ok"] is True


@pytest.mark.parametrize(
    "pg, ch",
    [
        ({"pending": ["migrate-063-x.sql"]}, {}),
        ({"drift": ["migrate-004-snmp.sql"]}, {}),
        ({"failed": [{"filename": "migrate-063-x.sql", "error": "syntax"}]}, {}),
        ({"error": "runner did not start"}, {}),
        ({}, {"pending": ["migrate-058-apm-synthetics-clickhouse.sql"]}),
        ({}, {"failed": [{"filename": "x-clickhouse.sql", "error": "boom"}]}),
        ({}, {"unresolved": [{"filename": "y-clickhouse.sql", "reason": "partial", "missing": ["zenplus.t"]}]}),
        ({}, {"error": "ClickHouse unreachable"}),
    ],
)
def test_any_unresolved_state_fails_the_gate(pg, ch):
    status = sync_schema.summarize(pg, ch)

    assert status["ok"] is False
    assert status["problems"]


def test_unreachable_database_is_never_reported_as_healthy():
    """"Cannot tell" must not read as "up to date" — that conflation is what
    let an appliance take a version stamp it could not support."""
    status = sync_schema.summarize({}, {"error": "ClickHouse unreachable: timeout"})

    assert status["ok"] is False
    assert any("unreachable" in p.lower() for p in status["problems"])


def test_postgres_report_is_read_from_the_json_marker(monkeypatch, tmp_path):
    payload = {"applied": ["migrate-063-x.sql"], "skipped": [], "pending": [],
               "drift": [], "failed": []}

    class Result:
        returncode = 0
        stdout = (
            "apply   migrate-063-x.sql\n"
            + sync_schema.JSON_MARKER + __import__("json").dumps(payload) + "\n"
        )
        stderr = ""

    monkeypatch.setattr(sync_schema.subprocess, "run", lambda *a, **k: Result())

    assert sync_schema.run_postgres(tmp_path, check_only=False) == payload


def test_a_runner_that_cannot_start_is_an_error_not_a_clean_run(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise OSError("sudo: command not found")

    monkeypatch.setattr(sync_schema.subprocess, "run", boom)
    report = sync_schema.run_postgres(tmp_path, check_only=False)

    assert "error" in report
    assert sync_schema.summarize(report, {})["ok"] is False


def test_dry_run_does_not_create_the_ledger_table(tmp_path):
    """--check must be read-only, including when the ledger does not exist yet."""
    scripts = _scripts(tmp_path, {"migrate-004-snmp-clickhouse.sql": SNMP_SQL})
    writes: list[str] = []

    def query(sql, timeout=300):
        stripped = sql.strip()
        if stripped.upper().startswith("SELECT NAME FROM SYSTEM.TABLES"):
            return ""
        if "FROM zenplus.schema_migrations" in stripped:
            raise ch_migrate.ClickHouseError("Table zenplus.schema_migrations does not exist")
        writes.append(stripped)
        return ""

    summary = ch_migrate.sync(scripts, query=query, dry_run=True)

    assert writes == []
    assert summary["pending"] == ["migrate-004-snmp-clickhouse.sql"]


# ─── String literals must not break statement analysis ───────────────────────
#
# Regression for a shipped failure: migrate-063 seeds templates with INSERT …
# ON CONFLICT, but its JSON payload contained "only; empty". _INSERT_RE ends a
# statement at the first ';', so the ON CONFLICT fell outside the captured body,
# the INSERT read as unguarded, and the migration was refused as unverifiable —
# failing the update at step 9/24 on every appliance and rolling it back.

_SEED_WITH_SEMICOLON_IN_LITERAL = """
INSERT INTO device_profiles (name, oid_groups, builtin)
VALUES (
  'Juniper JunOS',
  $oidg$ [{"key":"srx_flow","description":"flow-mode SRX only; empty on EX/MX"}] $oidg$::jsonb,
  TRUE
)
ON CONFLICT (name, version) DO UPDATE SET oid_groups = EXCLUDED.oid_groups;
"""

_SEED_UNGUARDED = """
INSERT INTO device_profiles (name) VALUES ('no guard here; really');
"""

_DO_BLOCK_GUARDED = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'x_fkey') THEN
        ALTER TABLE devices ADD CONSTRAINT x_fkey FOREIGN KEY (p) REFERENCES q(id);
    END IF;
END $$;
"""

_DO_BLOCK_UNGUARDED = """
DO $$
BEGIN
    ALTER TABLE devices ADD COLUMN surprise TEXT;
END $$;
"""


def test_semicolon_inside_literal_does_not_hide_on_conflict():
    """A guarded seed stays guarded even when its payload contains a semicolon."""
    assert migration_order.writes_rows(_SEED_WITH_SEMICOLON_IN_LITERAL) is False
    assert migration_order.is_replay_safe(_SEED_WITH_SEMICOLON_IN_LITERAL) is True


def test_unguarded_insert_still_detected():
    """The guard must not be so lenient that a bare INSERT slips through."""
    assert migration_order.writes_rows(_SEED_UNGUARDED) is True
    assert migration_order.is_replay_safe(_SEED_UNGUARDED) is False


def test_do_block_ddl_replay_safety():
    """DDL inside a DO block is judged on the block's own IF NOT EXISTS probe."""
    assert migration_order.is_replay_safe(_DO_BLOCK_GUARDED) is True
    assert migration_order.is_replay_safe(_DO_BLOCK_UNGUARDED) is False


_TEMP_TABLE_STAGING = """
CREATE TEMP TABLE _stub_migration (stub text, pack text) ON COMMIT DROP;
INSERT INTO _stub_migration (stub, pack) VALUES ('old', 'new');
UPDATE devices d SET profile_id = m.pack FROM _stub_migration m WHERE d.name = m.stub;
"""

_TEMP_TABLE_PLUS_REAL_SEED = """
CREATE TEMPORARY TABLE _scratch (a int);
INSERT INTO _scratch VALUES (1);
INSERT INTO device_profiles (name) VALUES ('seeded');
"""


def test_writing_to_a_temp_table_is_not_writing_rows():
    """migrate-064 staged its transform in two ON COMMIT DROP temp tables.

    Reading those as persistent seed writes made a fully idempotent migration
    look unverifiable, so the runner refused to run it and the schema gate
    failed the update on every appliance that had not already applied it.
    """
    assert migration_order.temp_tables(_TEMP_TABLE_STAGING) == {"_stub_migration"}
    assert migration_order.writes_rows(_TEMP_TABLE_STAGING) is False


def test_a_real_seed_alongside_a_temp_table_still_counts():
    """The exemption is per-target, not per-file."""
    assert migration_order.writes_rows(_TEMP_TABLE_PLUS_REAL_SEED) is True


def test_an_insert_into_an_undeclared_table_is_never_exempt():
    """Only tables this migration declares TEMP are scratch space."""
    assert migration_order.writes_rows("INSERT INTO _scratch VALUES (1);") is True


def test_every_shipped_postgres_migration_can_be_classified():
    """An appliance that skipped releases must be able to catch up in one pass.

    run-migrations.py refuses to run a migration that inserts rows and creates
    nothing to probe, and the gate turns that refusal into a failed update and a
    rollback. Any migration in that state strands every appliance that has not
    already applied it — with no way to ever converge.
    """
    unclassifiable = []
    for path in migration_order.ordered_migrations(SCRIPTS_DIR, engine="postgres"):
        sql = path.read_text()
        if migration_order.writes_rows(sql) and not migration_order.created_objects(sql):
            unclassifiable.append(path.name)

    assert unclassifiable == []


def test_analyzable_neutralises_literals_but_keeps_structure():
    out = migration_order.analyzable(_SEED_WITH_SEMICOLON_IN_LITERAL)
    assert "only; empty" not in out
    assert "ON CONFLICT" in out
    assert "INSERT INTO device_profiles" in out


def test_shipped_template_seeds_are_runnable():
    """The two real template migrations must be verifiable by the runner."""
    scripts = Path("/opt/zenplus/scripts")
    for name in ("migrate-062-monitoring-templates.sql",
                 "migrate-063-templates-juniper-aruba.sql"):
        path = scripts / name
        if not path.exists():
            pytest.skip(f"{name} not present")
        sql = path.read_text()
        assert migration_order.writes_rows(sql) is False, f"{name} reads as unguarded"
        assert migration_order.is_replay_safe(sql) is True, f"{name} reads as unsafe to replay"
