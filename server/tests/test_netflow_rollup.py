"""NetFlow rollups: schema v2 migration, view mirrors, healer planning and repair.

Unit tests cover the SQL the healer generates, its planning rules and the
migration's static properties. Live tests run against a scratch ClickHouse
(NETFLOW_TEST_CH_URL, default http://default:scratchpw@127.0.0.1:18123) in
private databases they create and drop, and are skipped when it is not
reachable. The central live property: for the same raw rows, every rollup the
materialized views produce equals what the healer derives from raw.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import pytest

from app.services import netflow_rollup as nr

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
import ch_migrate  # noqa: E402
import migration_order  # noqa: E402

MIGRATION = "migrate-137-netflow-schema-v2-clickhouse.sql"
MIGRATION_SQL = (SCRIPTS / MIGRATION).read_text(encoding="utf-8")
# The NetFlow ClickHouse migrations in release (lock) order.
UPGRADE_PATH = (
    "migrate-20260506-netflow-clickhouse.sql",
    "migrate-071-flow-conversations-rollup-clickhouse.sql",
    "migrate-072-flow-qos-rollup-clickhouse.sql",
    MIGRATION,
)
# migrate-144 (block log on every rollup table) ships with 137; live databases
# get the full release path so the healer runs under production settings.
DEDUP_MIGRATION = "migrate-144-netflow-rollup-dedup-clickhouse.sql"
DEDUP_SQL = (SCRIPTS / DEDUP_MIGRATION).read_text(encoding="utf-8")
RELEASE_PATH = UPGRADE_PATH + (DEDUP_MIGRATION,)
H = 3600

# clickhouse_connect decodes every DateTime through datetime.utcfromtimestamp.
pytestmark = pytest.mark.filterwarnings("ignore:datetime.datetime.utcfromtimestamp:DeprecationWarning")


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


# ─── Migration: static properties ────────────────────────────────────────────


def test_migration_is_replay_safe_and_writes_no_rows():
    assert migration_order.is_replay_safe(MIGRATION_SQL)
    assert not migration_order.writes_rows(MIGRATION_SQL)
    assert migration_order.engine_of(MIGRATION) == "clickhouse"


def test_schema_gate_can_prove_the_migration_ran():
    objects = {o.lower() for o in migration_order.created_objects(MIGRATION_SQL)}
    for table in ("flow_interface_5m", "flow_exporter_minutes", "flow_collector_stats",
                  "flow_templates", "flow_app_names", "flow_if_counters",
                  "flow_interface_5m_mv", "flow_exporter_minutes_mv"):
        assert f"zenplus.{table}" in objects
    columns = migration_order.added_columns(MIGRATION_SQL)
    for column in ("input_if", "output_if", "ip_version", "exporter_ip6", "src_addr6", "dst_addr6",
                   "next_hop6", "fields_present", "observation_domain", "flow_direction",
                   "export_protocol", "app_id", "fw_event", "xlate_src_addr6", "xlate_dst_addr6",
                   "xlate_src_port", "xlate_dst_port", "flow_end_reason"):
        assert f"flow_records.{column}" in columns
    for column in ("exporter_ip6", "src_addr6", "dst_addr6", "input_ifs", "output_ifs",
                   "flagged_flows", "legacy_flows", "duration_valid_flows", "duration_valid_ms", "app_id"):
        assert f"flow_conversations_1h.{column}" in columns
    for column in ("exporter_ip6", "flags_known", "tos_known", "duration_valid_flows", "duration_valid_ms"):
        assert f"flow_qos_1h.{column}" in columns


def test_every_view_the_migration_drops_it_recreates():
    """The schema sync re-runs a recorded migration whose objects are missing.
    Dropping a view an earlier migration created, without recreating that name,
    would make the sync resurrect the old view next to the new one."""
    stripped = migration_order.analyzable(MIGRATION_SQL)
    dropped = {m.lower() for m in re.findall(r"DROP\s+VIEW\s+IF\s+EXISTS\s+([\w.]+)", stripped, re.I)}
    created = {o.lower() for o in migration_order.created_objects(MIGRATION_SQL)}
    assert dropped and dropped <= created
    earlier = set()
    for name in UPGRADE_PATH[:-1]:
        earlier |= {o.lower() for o in migration_order.created_objects((SCRIPTS / name).read_text())}
    assert earlier <= created | {"zenplus.flow_records", "zenplus.flow_traffic_5m",
                                 "zenplus.flow_conversations_1h", "zenplus.flow_qos_1h"}


class _SchemaFake:
    """ClickHouse stand-in for ch_migrate that models DROP as well as CREATE."""

    def __init__(self):
        self.objects: set[str] = set()
        self.columns: set[str] = set()
        self.ledger: dict[str, str] = {}
        self.settings: dict[str, dict[str, str]] = {}

    def __call__(self, sql: str, timeout: int = 300) -> str:
        s = sql.strip()
        up = s.upper()
        if up.startswith("SELECT NAME, CREATE_TABLE_QUERY FROM SYSTEM.TABLES"):
            # ch_migrate.missing_table_settings: report each table's SETTINGS.
            names = re.findall(r"'([^']+)'", s.split(" IN (", 1)[1])
            return "".join(
                json.dumps({"name": n, "create_table_query": "CREATE TABLE zenplus." + n + " SETTINGS "
                            + ", ".join(f"{k} = {v}" for k, v in self.settings.get(n, {}).items())}) + "\n"
                for n in names if f"zenplus.{n}" in self.objects
            )
        if up.startswith("SELECT NAME FROM SYSTEM.TABLES"):
            return "".join(o.split(".", 1)[1] + "\n" for o in sorted(self.objects))
        if up.startswith("SELECT TABLE, NAME FROM SYSTEM.COLUMNS"):
            return "".join(c.replace(".", "\t", 1) + "\n" for c in sorted(self.columns))
        if "FROM ZENPLUS.SCHEMA_MIGRATIONS" in up:
            return "".join(f"{k}\t{v}\n" for k, v in self.ledger.items())
        if up.startswith("INSERT INTO ZENPLUS.SCHEMA_MIGRATIONS"):
            self.ledger[s.split("VALUES ('", 1)[1].split("'", 1)[0]] = "x"
            return ""
        if "SCHEMA_MIGRATIONS" in up:
            return ""
        for stmt in migration_order.analyzable(s).split(";"):
            drop = re.search(r"DROP\s+(?:VIEW|TABLE)\s+IF\s+EXISTS\s+([\w.]+)", stmt, re.I)
            if drop:
                self.objects.discard(drop.group(1).lower())
            for obj in migration_order.created_objects(stmt):
                self.objects.add(obj.lower())
            self.columns |= migration_order.added_columns(stmt)
            setting = re.search(r"ALTER\s+TABLE\s+zenplus\.(\w+)\s+MODIFY\s+SETTING\s+(\w+)\s*=\s*(\w+)", stmt, re.I)
            if setting:
                self.settings.setdefault(setting.group(1), {})[setting.group(2)] = setting.group(3)
        return ""


def test_schema_sync_does_not_resurrect_superseded_views(tmp_path):
    for name in RELEASE_PATH:
        (tmp_path / name).write_text((SCRIPTS / name).read_text())
    lock = [line for line in (SCRIPTS / "migrations.lock").read_text().splitlines()
            if line.split()[-1:] and line.split()[-1] in RELEASE_PATH]
    (tmp_path / "migrations.lock").write_text("\n".join(lock) + "\n")
    fake = _SchemaFake()
    first = ch_migrate.sync(tmp_path, query=fake)
    assert sorted(first["applied"]) == sorted(RELEASE_PATH)
    assert all(fake.settings[s.table] == {"non_replicated_deduplication_window": "1000"} for s in nr.ROLLUPS)
    second = ch_migrate.sync(tmp_path, query=fake)
    assert second["applied"] == second["healed"] == second["failed"] == []


def test_views_apply_the_volume_guard_the_healer_uses():
    guard = _norm(nr.VOLUME_GUARD)
    views = re.split(r"CREATE MATERIALIZED VIEW", MIGRATION_SQL)[1:]
    assert len(views) == 5
    for view in views:
        assert guard in _norm(view)


def test_x02_mutation_matches_the_migration():
    stmt = next(s for s in MIGRATION_SQL.split(";") if "UPDATE" in s and "flow_records" in s)
    body = _norm(stmt[stmt.index("ALTER TABLE"):])
    assert body == _norm(nr.x02_mutation_sql("zenplus")) + " SETTINGS mutations_sync = 0"


# ─── Healer SQL ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("spec", nr.ROLLUPS, ids=lambda s: s.table)
def test_every_insert_names_its_columns(spec):
    sql = spec.insert_sql("zenplus")
    head = re.match(r"INSERT INTO zenplus\.(\w+) \(([^)]*)\)\nSELECT\n", sql)
    assert head and head.group(1) == spec.table
    columns = [c.strip() for c in head.group(2).split(",")]
    assert columns == [c for c, _ in spec.out]
    assert len(set(columns)) == len(columns)
    assert "{db}" not in sql and "SELECT *" not in sql


@pytest.mark.parametrize("spec", nr.ROLLUPS, ids=lambda s: s.table)
def test_every_insert_is_bounded_to_one_window(spec):
    sql = spec.insert_sql("zenplus")
    if spec.clock == "received":
        assert "received_at >= %(lo)s AND received_at < %(hi)s" in sql
        assert "%(ts_lo_ms)s" in sql and "%(ts_hi_ms)s" in sql
    elif spec.lookahead_h:
        assert "timestamp >= %(lo)s AND timestamp < %(raw_hi)s" in sql
        assert "slot_ms >= %(slot_lo_ms)s AND slot_ms < %(slot_hi_ms)s" in sql
    else:
        assert "timestamp >= %(lo)s AND timestamp < %(hi)s" in sql


def test_every_rollup_the_healer_writes_has_the_block_log():
    """migrate-144: the block log on each view target is what makes a collector
    retry idempotent in the rollups; ch_migrate verifies it on every appliance."""
    assert migration_order.is_replay_safe(DEDUP_SQL)
    assert not migration_order.writes_rows(DEDUP_SQL)
    assert migration_order.engine_of(DEDUP_MIGRATION) == "clickhouse"
    altered = set(re.findall(
        r"ALTER TABLE zenplus\.(\w+)\s+MODIFY SETTING non_replicated_deduplication_window = 1000;",
        migration_order.analyzable(DEDUP_SQL)))
    tables = {spec.table for spec in nr.ROLLUPS}
    assert altered == tables
    spec = ch_migrate.TABLE_SETTING_INVARIANTS[DEDUP_MIGRATION]
    assert set(spec["tables"]) == tables
    assert (spec["setting"], spec["value"]) == ("non_replicated_deduplication_window", "1000")


def test_healer_inserts_never_deduplicate_by_content(monkeypatch):
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(t, sql, params=None, settings=None):
        calls.append((sql, settings))

    monkeypatch.setattr(nr, "_exec", fake_exec)
    spec = nr.ROLLUPS[0]
    asyncio.run(nr.rebuild_hour(nr.ChTarget(database="db"), spec, 0, {"lo": 0, "hi": 1}, has_rows=True))
    delete, insert = calls
    assert "DELETE WHERE" in delete[0]
    assert insert[0].startswith(f"INSERT INTO db.{spec.table}")
    assert insert[1] == {"insert_deduplicate": 0}


def test_window_params_cover_the_raw_each_rollup_reads():
    hour = 1_759_230_000 - 1_759_230_000 % H
    p = nr._window_params(nr.INTERFACE_5M, hour, None)
    assert p["raw_hi"] == nr._ch_time(hour + 2 * H)
    assert (p["slot_lo_ms"], p["slot_hi_ms"]) == (hour * 1000, (hour + H) * 1000)
    hint = {"total": 5, "ts_min_ms": (hour - 5 * H) * 1000, "ts_max_ms": (hour + H) * 1000}
    p = nr._window_params(nr.EXPORTER_MINUTES, hour, hint)
    assert p["ts_lo_ms"] == (hour - 5 * H) * 1000          # a skewed exporter widens the bound
    assert p["ts_hi_ms"] >= (hour + H) * 1000 + 120_000    # never narrower than the clamp window


# ─── Retention floor (X-11) ──────────────────────────────────────────────────

NOW = 1_759_240_000


def test_floor_excludes_the_oldest_raw_hour():
    oldest = NOW - 5 * H - 10
    assert nr.retention_floor(NOW, oldest_raw_s=oldest, raw_retention_days=None,
                              rollup_ttl_days=None) == oldest - oldest % H + H


def test_floor_follows_the_configured_raw_retention():
    floor = nr.retention_floor(NOW, oldest_raw_s=NOW - 40 * 86400, raw_retention_days=5,
                               rollup_ttl_days=None)
    assert floor >= NOW - 5 * 86400 + nr.RETENTION_MARGIN_S
    assert floor % H == 0 and floor - (NOW - 5 * 86400 + nr.RETENTION_MARGIN_S) < H


def test_floor_respects_a_shorter_rollup_ttl():
    floor = nr.retention_floor(NOW, oldest_raw_s=NOW - 40 * 86400, raw_retention_days=30,
                               rollup_ttl_days=7)
    assert floor >= NOW - 7 * 86400


def test_floor_is_none_without_raw():
    assert nr.retention_floor(NOW, oldest_raw_s=None, raw_retention_days=30, rollup_ttl_days=None) is None


def test_truncated_hour_below_the_floor_is_never_healed():
    """The X-11 reproduction: raw lost part of an hour to TTL, the rollup kept it."""
    hour = NOW - NOW % H - 5 * 86400
    floor = nr.retention_floor(NOW, oldest_raw_s=hour + 100, raw_retention_days=5, rollup_ttl_days=400)
    plan = nr.plan_rollup(nr.CONVERSATIONS, {hour: _flow(flows=32)},
                          {hour: nr.RolledHour(rows=60, count=60, bytes=0)}, now_s=NOW, floor_s=floor)
    assert plan.items == []


# ─── Planning ────────────────────────────────────────────────────────────────


def _flow(flows=0, if_sides=None, bytes_=0, v5=0):
    return {"flows": flows, "if_sides": flows * 2 if if_sides is None else if_sides,
            "bytes": bytes_, "v5_sampled": v5}


def _recv(counted=0, total=None):
    return {"counted": counted, "total": counted if total is None else total, "bytes": 0,
            "ts_min_ms": 0, "ts_max_ms": 0}


CUR = NOW - NOW % H


def test_plan_backfills_missing_hours_heals_mismatches_and_skips_converged():
    raw = {CUR - 5 * H: _flow(10), CUR - 4 * H: _flow(10), CUR - 3 * H: _flow(10)}
    rolled = {CUR - 4 * H: nr.RolledHour(3, 7, 0), CUR - 3 * H: nr.RolledHour(3, 10, 0)}
    plan = nr.plan_rollup(nr.CONVERSATIONS, raw, rolled, now_s=NOW, floor_s=CUR - 10 * H)
    assert [(p.hour, p.reason) for p in plan.items] == [(CUR - 5 * H, "backfill"), (CUR - 4 * H, "heal")]


def test_plan_heals_a_double_counted_hour_too():
    raw = {CUR - 4 * H: _flow(10)}
    plan = nr.plan_rollup(nr.QOS, raw, {CUR - 4 * H: nr.RolledHour(2, 11, 0)}, now_s=NOW,
                          floor_s=CUR - 10 * H)
    assert [p.reason for p in plan.items] == ["heal"]


def test_plan_caps_heals_per_sweep():
    raw = {CUR - k * H: _flow(10) for k in range(3, 20)}
    rolled = {h: nr.RolledHour(1, 1, 0) for h in raw}
    plan = nr.plan_rollup(nr.CONVERSATIONS, raw, rolled, now_s=NOW, floor_s=CUR - 30 * H, max_heals=6)
    assert len(plan.items) == 6 and {p.reason for p in plan.items} == {"heal"}


def test_plan_defers_unsettled_hours_and_reports_when_they_are_ready():
    raw = {CUR - H: _flow(10)}
    plan = nr.plan_rollup(nr.CONVERSATIONS, raw, {CUR - H: nr.RolledHour(1, 4, 0)}, now_s=NOW,
                          floor_s=CUR - 10 * H)
    assert plan.items == [] and plan.next_ready_s == CUR + H
    # A missing (not mismatched) hour may be back-filled as soon as it is over.
    plan = nr.plan_rollup(nr.CONVERSATIONS, raw, {}, now_s=NOW, floor_s=CUR - 10 * H)
    assert [p.reason for p in plan.items] == ["backfill"]


def test_interface_backfill_waits_for_the_spread_back_hour_and_rebuilds_the_neighbour():
    raw = {CUR - 2 * H: _flow(5), CUR - 3 * H: _flow(5), CUR - 6 * H: _flow(5)}
    plan = nr.plan_rollup(nr.INTERFACE_5M, raw, {}, now_s=NOW, floor_s=CUR - 10 * H)
    assert [(p.hour, p.reason) for p in plan.items] == [
        (CUR - 7 * H, "neighbor"), (CUR - 6 * H, "backfill"),
        (CUR - 4 * H, "neighbor"), (CUR - 3 * H, "backfill"),
    ]
    assert plan.next_ready_s == CUR - 2 * H + 3 * H


def test_interface_absence_is_judged_on_end_slot_flows_not_rows():
    raw = {CUR - 5 * H: _flow(4)}
    rolled = {CUR - 5 * H: nr.RolledHour(rows=12, count=0, bytes=0)}   # spread-back rows only
    plan = nr.plan_rollup(nr.INTERFACE_5M, raw, rolled, now_s=NOW, floor_s=CUR - 10 * H)
    assert (CUR - 5 * H, "backfill") in [(p.hour, p.reason) for p in plan.items]


def test_exporter_minutes_backfills_hours_that_only_hold_unguarded_records():
    raw = {CUR - 5 * H: _recv(counted=0, total=3)}
    plan = nr.plan_rollup(nr.EXPORTER_MINUTES, raw, {}, now_s=NOW, floor_s=CUR - 10 * H)
    assert [p.reason for p in plan.items] == ["backfill"]
    converged = {CUR - 5 * H: nr.RolledHour(rows=2, count=0, bytes=0)}
    assert nr.plan_rollup(nr.EXPORTER_MINUTES, raw, converged, now_s=NOW,
                          floor_s=CUR - 10 * H).items == []


def test_plan_repairs_queued_hours_and_gives_up_on_expired_ones():
    raw = {CUR - 5 * H: _flow(10), CUR - 4 * H: _flow(0, if_sides=0)}
    rolled = {CUR - 5 * H: nr.RolledHour(3, 10, 0), CUR - 4 * H: nr.RolledHour(2, 5, 0)}
    queued = {CUR - 40 * H, CUR - 5 * H, CUR - 4 * H, CUR + H}
    plan = nr.plan_rollup(nr.CONVERSATIONS, raw, rolled, now_s=NOW, floor_s=CUR - 20 * H, queued=queued)
    assert [(p.hour, p.reason) for p in plan.items] == [(CUR - 5 * H, "repair")]
    # Below the floor, or raw no longer holding the hour: cannot be corrected.
    assert sorted(plan.unrecoverable) == [CUR - 40 * H, CUR - 4 * H]
    # A future hour (a skewed exporter clock) stays queued until it settles.
    assert plan.next_ready_s == CUR + 3 * H


def test_still_needed_rechecks_under_the_lock():
    spec = nr.CONVERSATIONS
    assert nr.still_needed(spec, "backfill", nr.HourState(10, 10, 0, 0))
    assert not nr.still_needed(spec, "backfill", nr.HourState(10, 10, 3, 3))   # the view got there first
    assert nr.still_needed(spec, "heal", nr.HourState(10, 10, 3, 9))
    assert not nr.still_needed(spec, "heal", nr.HourState(10, 10, 3, 10))
    assert not nr.still_needed(spec, "repair", nr.HourState(0, 0, 3, 10))
    assert nr.still_needed(nr.INTERFACE_5M, "backfill", nr.HourState(4, 4, 7, 0))


def test_repair_queue_bookkeeping():
    state = nr._empty_repair_state()
    assert nr._enqueue(state, "flow_qos_1h", [3 * H, H, 3 * H]) == 2
    assert nr._enqueue(state, "flow_qos_1h", [H]) == 0
    nr._dequeue(state, "flow_qos_1h", H, "rebuilt")
    nr._dequeue(state, "flow_qos_1h", 3 * H, "unrecoverable")
    assert state["x02"]["queue"] == {}
    assert state["x02"]["rebuilt"] == {"flow_qos_1h": 1}
    assert state["x02"]["unrecoverable"] == {"flow_qos_1h": 1}
    json.dumps(state)


# ─── Live ClickHouse ─────────────────────────────────────────────────────────

CH_URL = os.environ.get("NETFLOW_TEST_CH_URL", "http://default:scratchpw@127.0.0.1:18123")


def _connect(database: str = "default"):
    import clickhouse_connect

    u = urlparse(CH_URL)
    return clickhouse_connect.get_client(
        host=u.hostname, port=u.port or 8123, username=u.username or "default",
        password=u.password or "", database=database,
    )


def _reachable() -> bool:
    try:
        _connect().command("SELECT 1")
        return True
    except Exception:
        return False


live = pytest.mark.skipif(not _reachable(), reason=f"scratch ClickHouse not reachable at {CH_URL}")


def _statements(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def _apply(client, database: str, *files: str) -> None:
    for name in files:
        for stmt in _statements((SCRIPTS / name).read_text().replace("zenplus.", f"{database}.")):
            if stmt.upper().startswith("CREATE DATABASE"):
                continue
            client.command(stmt)


def _target(database: str) -> nr.ChTarget:
    local = threading.local()

    def client():
        c = getattr(local, "c", None)
        if c is None:
            c = local.c = _connect(database)
        return c

    return nr.ChTarget(database=database, client=client)


@pytest.fixture
def databases():
    """Private databases, dropped afterwards."""
    admin = _connect()
    made: list[str] = []

    def make(*files: str, drop_views: bool = False) -> str:
        name = f"nft_{uuid.uuid4().hex[:10]}"
        admin.command(f"CREATE DATABASE {name}")
        made.append(name)
        _apply(admin, name, *files)
        # The deterministic arithmetic/replay vectors use a fixed calendar
        # date. Production TTL would eventually delete those rows mid-test,
        # making results depend on the day the suite runs. Disable expiry in
        # these private tables; the dedicated TTL-edge test installs its own
        # retention policy and uses timestamps relative to the current clock.
        for (table,) in admin.query(
            f"SELECT name FROM system.tables WHERE database = '{name}' "
            "AND engine LIKE '%MergeTree' AND create_table_query LIKE '% TTL %'"
        ).result_rows:
            admin.command(f"ALTER TABLE {name}.{table} REMOVE TTL")
        if drop_views:
            for (view,) in admin.query(
                f"SELECT name FROM system.tables WHERE database = '{name}' AND engine = 'MaterializedView'"
            ).result_rows:
                admin.command(f"DROP VIEW {name}.{view}")
        return name

    yield make
    for name in made:
        admin.command(f"DROP DATABASE IF EXISTS {name}")


RAW_COLUMNS = (
    "timestamp", "received_at", "collector_id", "exporter_ip", "flow_version", "sampling_interval",
    "src_addr", "dst_addr", "input_snmp", "output_snmp", "packets", "bytes", "first_switched_ms",
    "last_switched_ms", "src_port", "dst_port", "tcp_flags", "protocol", "tos",
    "input_if", "output_if", "ip_version", "exporter_ip6", "src_addr6", "dst_addr6",
    "fields_present", "observation_domain", "flow_direction", "export_protocol", "app_id", "fw_event",
)
V1_COLUMNS = RAW_COLUMNS[:19]
TRACKED = (1 << 31) | 0x1FF


def _generate_rows(base: datetime, hours: int = 6, count: int = 1600, seed: int = 7) -> list[tuple]:
    """Raw records that exercise every rollup rule: IPv4/IPv6 flows and
    exporters, legacy and tracked field presence, NSEL events, corrupt counters,
    32-bit ifIndexes, ifIndex 0, every kind of duration (none, short, spread
    over hours, clamped, invalid, mixed clocks), hour-boundary end times, clock
    lag and sampling. src ports stay below 10 per key so groupUniqArray(10) is
    deterministic."""
    rng = random.Random(seed)
    exporters = [
        ("10.20.0.1", "::", TRACKED, 9),         # tracked v9
        ("10.20.0.2", "::", 0, 5),               # legacy v5
        ("0.0.0.0", "fd00::20", TRACKED, 10),    # IPv6 exporter (IPFIX)
        ("10.20.0.3", "::", TRACKED & ~1, 9),    # ASA-like: flags not exported, NSEL events
    ]
    rows = []
    for i in range(count):
        exp, exp6, present, version = exporters[i % 4]
        end = base + timedelta(seconds=rng.randrange(hours * 3600), milliseconds=rng.randrange(1000))
        if i % 97 == 0:
            end = base + timedelta(hours=rng.randrange(1, hours))            # exactly on an hour
        v6 = rng.random() < 0.25
        src, dst = (("0.0.0.0", "0.0.0.0") if v6 else
                    (f"10.1.{rng.randrange(3)}.{rng.randrange(1, 5)}", f"10.9.{rng.randrange(2)}.{rng.randrange(1, 4)}"))
        src6, dst6 = ((f"2001:db8::{rng.randrange(1, 4)}", f"2001:db8:ff::{rng.randrange(1, 3)}") if v6 else ("::", "::"))
        kind = rng.randrange(8)
        uptime_end = 10_000_000 + i
        if kind == 0:
            first, last = uptime_end, uptime_end                            # zero duration
        elif kind == 1:
            first, last = uptime_end - rng.randrange(1, 290_000), uptime_end
        elif kind == 2:
            first, last = uptime_end - rng.randrange(300_000, 3_000_000), uptime_end   # spans slots/hours
        elif kind == 3:
            first, last = uptime_end - 7_200_000, uptime_end                # clamped to 1 h
        elif kind == 4:
            first, last = 0, uptime_end                                     # absent start
        elif kind == 5:
            first, last = uptime_end + 5, uptime_end                        # inverted
        elif kind == 6:
            epoch_end = int(end.timestamp() * 1000)
            first, last = epoch_end - rng.randrange(1, 600_000), epoch_end  # IPFIX epoch clock
        else:
            first, last = 5_000, int(end.timestamp() * 1000)                # mixed clocks
        protocol = rng.choice((6, 6, 17, 1))
        packets = rng.randrange(0, 50)
        bytes_ = packets * rng.randrange(40, 1500)
        sampling = 1 if version != 5 else rng.choice((1, 100))
        if i % 211 == 0:
            bytes_ = 5 * 10 ** 15                                           # corrupt counter
        fw_event = rng.choice((1, 2, 3)) if exp == "10.20.0.3" and rng.random() < 0.6 else 0
        input_if = rng.choice((0, 1, 2, 70000))
        output_if = rng.choice((0, 3, 131072))
        lag = rng.choice((200, 1500, 45_000, -30_000, 3 * 3600 * 1000))
        rows.append((
            end, end + timedelta(milliseconds=lag), "t", exp, version, sampling,
            src, dst, min(input_if, 65535), min(output_if, 65535), packets, bytes_ * sampling,
            first, last, rng.choice((40000, 40001, 40002, 53, 443)), rng.choice((443, 53, 80, 0)),
            rng.randrange(0, 64) if present & 1 or present == 0 else 0, protocol, rng.choice((0, 46 << 2)),
            input_if, output_if, 6 if v6 else 4, exp6, src6, dst6,
            present, rng.choice((0, 256)) if version == 5 else 0, rng.choice((0, 1, 255)),
            {5: "netflow5", 9: "netflow9", 10: "ipfix"}[version], rng.choice((0, 0, (13 << 32) | 80)),
            fw_event,
        ))
    return rows


_CANON = {
    "flow_conversations_1h": """
        SELECT timestamp, exporter_ip, src_addr, dst_addr, protocol, dst_port, exporter_ip6, src_addr6, dst_addr6,
               sum(bytes), sum(packets), sum(flows),
               arraySort(groupUniqArrayMerge(10)(src_ports)), arraySort(groupUniqArrayMerge(10)(input_snmp)),
               arraySort(groupUniqArrayMerge(10)(output_snmp)), min(first_seen), max(last_seen), max(received_at),
               sum(duration_ms), groupBitOr(tcp_flags), arraySort(groupUniqArrayMerge(10)(input_ifs)),
               arraySort(groupUniqArrayMerge(10)(output_ifs)), sum(flagged_flows), sum(legacy_flows),
               sum(duration_valid_flows), sum(duration_valid_ms), max(app_id)
        FROM {db}.flow_conversations_1h GROUP BY ALL ORDER BY ALL""",
    "flow_qos_1h": """
        SELECT timestamp, exporter_ip, protocol, tos, tcp_flags, exporter_ip6, flags_known, tos_known,
               sum(bytes), sum(packets), sum(flows), sum(empty_flows), sum(duration_ms), max(received_at),
               sum(duration_valid_flows), sum(duration_valid_ms)
        FROM {db}.flow_qos_1h GROUP BY ALL ORDER BY ALL""",
    "flow_traffic_5m": """
        SELECT timestamp, exporter_ip, protocol, dst_port, sum(bytes), sum(packets), sum(flow_count)
        FROM {db}.flow_traffic_5m GROUP BY ALL ORDER BY ALL""",
    "flow_interface_5m": """
        SELECT timestamp, exporter_ip, exporter_ip6, ifindex, direction, flow_direction,
               sum(bytes), sum(packets), sum(flows)
        FROM {db}.flow_interface_5m GROUP BY ALL HAVING sum(flows) > 0 OR sum(bytes) > 1e-9 ORDER BY ALL""",
    "flow_exporter_minutes": """
        SELECT minute, exporter_ip, exporter_ip6, observation_domain, sum(records), sum(bytes), sum(tcp_records),
               sum(tcp_flagged_records), sum(v6_records), sum(sampled_records), sum(legacy_records),
               max(max_sampling), min(first_received_at), max(last_received_at), min(min_flow_ts),
               max(max_flow_ts), min(lag_ms_min), max(lag_ms_max)
        FROM {db}.flow_exporter_minutes GROUP BY ALL ORDER BY ALL""",
}


def _canon(client, database: str, table: str) -> list[tuple]:
    return client.query(_CANON[table].replace("{db}", database)).result_rows


def _assert_same(a: list[tuple], b: list[tuple], table: str) -> None:
    assert len(a) == len(b), f"{table}: {len(a)} rows vs {len(b)}"
    for ra, rb in zip(a, b):
        for va, vb in zip(ra, rb):
            if isinstance(va, float) or isinstance(vb, float):
                assert va == pytest.approx(vb, rel=1e-9, abs=1e-6), (table, ra, rb)
            else:
                assert va == vb, (table, ra, rb)


def _insert(client, database: str, rows: list[tuple], columns=RAW_COLUMNS, batches: int = 4) -> None:
    size = -(-len(rows) // batches)
    for k in range(0, len(rows), size):
        client.insert(f"{database}.flow_records", [r[:len(columns)] for r in rows[k:k + size]],
                      column_names=list(columns))


def _heal_everything(t: nr.ChTarget, spec: nr.RollupSpec, lo_s: int, hi_s: int) -> int:
    """Run the healer's own census, planner and rebuild, without Postgres."""
    async def run() -> int:
        now_s = hi_s + 6 * H
        raw = await nr.raw_census(t, spec.clock, lo_s, now_s - now_s % H)
        rolled = await nr.rollup_census(t, spec, lo_s, now_s - now_s % H)
        plan = nr.plan_rollup(spec, raw, rolled, now_s=now_s, floor_s=lo_s, max_heals=10_000)
        for item in plan.items:
            params = nr._window_params(spec, item.hour, raw.get(item.hour))
            state = await nr.hour_state(t, spec, item.hour, params)
            if nr.still_needed(spec, item.reason, state):
                await nr.rebuild_hour(t, spec, item.hour, params, has_rows=state.rows > 0)
        return len(plan.items)

    return asyncio.run(run())


BASE = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)


@live
def test_healer_output_equals_view_output_for_every_rollup(databases):
    by_view = databases(*RELEASE_PATH)
    by_healer = databases(*RELEASE_PATH, drop_views=True)
    rows = _generate_rows(BASE)
    client = _connect()
    _insert(client, by_view, rows)
    _insert(client, by_healer, rows, batches=1)
    lo = int(BASE.timestamp()) - 3 * H
    hi = int(BASE.timestamp()) + 12 * H
    t = _target(by_healer)
    for spec in nr.ROLLUPS:
        assert _heal_everything(t, spec, lo, hi) > 0, spec.table
        expected = _canon(client, by_view, spec.table)
        assert expected, spec.table
        _assert_same(expected, _canon(client, by_healer, spec.table), spec.table)
    # Converged: a second pass finds nothing to do and changes nothing.
    for spec in nr.ROLLUPS:
        before = _canon(client, by_healer, spec.table)
        assert _heal_everything(t, spec, lo, hi) == 0, spec.table
        assert _canon(client, by_healer, spec.table) == before


@live
def test_healer_repairs_a_partially_rolled_up_hour_exactly(databases):
    by_view = databases(*RELEASE_PATH)
    by_healer = databases(*RELEASE_PATH)
    rows = _generate_rows(BASE, seed=11)
    client = _connect()
    _insert(client, by_view, rows)
    # The healer database loses the views half-way through (an outage), then
    # gets them back: every rollup is left short for the affected hours.
    ddl = dict(client.query(
        f"SELECT name, create_table_query FROM system.tables WHERE database = '{by_healer}' "
        "AND engine = 'MaterializedView'").result_rows)
    half = len(rows) // 2
    _insert(client, by_healer, rows[:half], batches=2)
    for view in ddl:
        client.command(f"DROP VIEW {by_healer}.{view}")
    _insert(client, by_healer, rows[half:half + 300], batches=1)
    for create in ddl.values():
        client.command(create)
    _insert(client, by_healer, rows[half + 300:], batches=2)
    t = _target(by_healer)
    lo, hi = int(BASE.timestamp()) - 3 * H, int(BASE.timestamp()) + 12 * H
    for spec in nr.ROLLUPS:
        _heal_everything(t, spec, lo, hi)
        _assert_same(_canon(client, by_view, spec.table), _canon(client, by_healer, spec.table), spec.table)


def _insert_batch(client, database: str, rows: list[tuple], token: str) -> None:
    """One collector batch: a stable token plus dependent-view deduplication,
    exactly the settings poller/cmd/netflow-collector/clickhouse.go sends."""
    client.insert(f"{database}.flow_records", [r[:len(RAW_COLUMNS)] for r in rows],
                  column_names=list(RAW_COLUMNS),
                  settings={"insert_deduplication_token": token,
                            "deduplicate_blocks_in_dependent_materialized_views": 1})


@live
def test_a_retried_collector_batch_is_counted_once_in_every_rollup(databases):
    """An ambiguous insert failure or a spool replay re-sends a batch with the
    same token. Raw skips it (migrate-137); with migrate-144 every rollup does
    too, and a new batch (new token) is still counted."""
    first, second = _generate_rows(BASE, count=600, seed=21), _generate_rows(BASE, count=300, seed=22)
    reference = databases(*RELEASE_PATH)
    retried = databases(*RELEASE_PATH)
    client = _connect()
    _insert_batch(client, reference, first, "batch-1")
    _insert_batch(client, retried, first, "batch-1")
    _insert_batch(client, retried, first, "batch-1")            # the retry
    for spec in nr.ROLLUPS:
        _assert_same(_canon(client, reference, spec.table), _canon(client, retried, spec.table), spec.table)
    assert client.query(f"SELECT count() FROM {retried}.flow_records").result_rows[0][0] == len(first)
    once = client.query(f"SELECT sum(flows) FROM {reference}.flow_conversations_1h").result_rows[0][0]
    assert once > 0
    _insert_batch(client, reference, second, "batch-2")
    _insert_batch(client, retried, second, "batch-2")
    for spec in nr.ROLLUPS:
        _assert_same(_canon(client, reference, spec.table), _canon(client, retried, spec.table), spec.table)

    # Control: without migrate-144 the same retry is skipped in raw but counted
    # twice in the rollups (ClickHouse 24.10 still pushes the deduplicated
    # block through the views). This is the defect the migration fixes.
    legacy = databases(*UPGRADE_PATH)
    _insert_batch(client, legacy, first, "batch-1")
    _insert_batch(client, legacy, first, "batch-1")
    assert client.query(f"SELECT count() FROM {legacy}.flow_records").result_rows[0][0] == len(first)
    assert client.query(f"SELECT sum(flows) FROM {legacy}.flow_conversations_1h").result_rows[0][0] == 2 * once


@live
def test_healer_rebuild_of_an_unchanged_hour_is_not_deduplicated(databases):
    """The healer replaces an hour with DELETE + INSERT ... SELECT. With the
    block log of migrate-144 an unchanged hour is byte-identical to the block it
    replaces; content deduplication would drop it and leave the hour empty."""
    by_view = databases(*RELEASE_PATH)
    by_healer = databases(*RELEASE_PATH, drop_views=True)
    rows = _generate_rows(BASE, seed=31)
    client = _connect()
    _insert(client, by_view, rows)
    _insert(client, by_healer, rows, batches=1)
    t = _target(by_healer)
    lo, hi = int(BASE.timestamp()) - 3 * H, int(BASE.timestamp()) + 12 * H
    for spec in nr.ROLLUPS:
        _heal_everything(t, spec, lo, hi)
    async def rebuild_all(spec) -> None:
        # Every hour raw has on this rollup's clock, with the healer's own hints.
        raw = await nr.raw_census(t, spec.clock, lo, hi)
        assert raw, spec.table
        for hour in sorted(raw):
            params = nr._window_params(spec, hour, raw[hour])
            await nr.rebuild_hour(t, spec, hour, params, has_rows=True)

    for _ in range(2):                                          # twice: the second is pure replay
        for spec in nr.ROLLUPS:
            asyncio.run(rebuild_all(spec))
            expected = _canon(client, by_view, spec.table)
            assert expected, spec.table
            _assert_same(expected, _canon(client, by_healer, spec.table), spec.table)

    # Control: with ClickHouse's default insert settings the second rebuild of
    # an unchanged hour is dropped silently and the hour stays empty, which is
    # why the healer inserts with INSERT_SETTINGS.
    spec = next(s for s in nr.ROLLUPS if s.table == "flow_traffic_5m")
    hour = int(BASE.timestamp()) + 2 * H
    params = nr._window_params(spec, hour, None)
    count = f"SELECT count() FROM {by_healer}.{spec.table} WHERE timestamp >= %(lo)s AND timestamp < %(hi)s"
    delete = f"ALTER TABLE {by_healer}.{spec.table} DELETE WHERE timestamp >= %(lo)s AND timestamp < %(hi)s"
    rolled = client.query(count, parameters=params).result_rows[0][0]
    assert rolled > 0
    for expected in (rolled, 0):
        client.command(delete, parameters=params, settings={"mutations_sync": 1})
        client.command(spec.insert_sql(by_healer), parameters=params)
        assert client.query(count, parameters=params).result_rows[0][0] == expected
    asyncio.run(nr.rebuild_hour(t, spec, hour, params, has_rows=False))
    assert client.query(count, parameters=params).result_rows[0][0] == rolled


@live
def test_interface_spreading_math(databases):
    db = databases(*RELEASE_PATH)
    client = _connect()
    t0 = datetime(2026, 9, 20, 12, 7, 30, tzinfo=timezone.utc)
    base = dict(zip(RAW_COLUMNS, _generate_rows(BASE, count=1)[0]))

    def row(**kw):
        values = {**base, "exporter_ip": "10.77.0.1", "fw_event": 0, "fields_present": TRACKED,
                  "sampling_interval": 1, "input_if": 7, "output_if": 70000, **kw}
        return tuple(values[c] for c in RAW_COLUMNS)

    client.insert(f"{db}.flow_records", [
        # 10 minutes ending 12:07:30 -> 1/4, 1/2, 1/4 over 11:55, 12:00, 12:05
        row(timestamp=t0, received_at=t0, bytes=600_000, packets=600,
            first_switched_ms=1_000_000, last_switched_ms=1_600_000),
        # zero duration ending 12:03 -> all in 12:00
        row(timestamp=t0 - timedelta(minutes=4, seconds=30), received_at=t0, bytes=1000, packets=1,
            first_switched_ms=5000, last_switched_ms=5000, output_if=0),
    ], column_names=list(RAW_COLUMNS))
    got = client.query(
        f"SELECT toString(timestamp), direction, ifindex, sum(bytes), sum(flows) FROM {db}.flow_interface_5m "
        "WHERE exporter_ip = '10.77.0.1' GROUP BY ALL ORDER BY ALL").result_rows
    assert got == [
        ("2026-09-20 11:55:00", 0, 7, 150_000.0, 0), ("2026-09-20 11:55:00", 1, 70000, 150_000.0, 0),
        ("2026-09-20 12:00:00", 0, 7, 301_000.0, 1), ("2026-09-20 12:00:00", 1, 70000, 300_000.0, 0),
        ("2026-09-20 12:05:00", 0, 7, 150_000.0, 1), ("2026-09-20 12:05:00", 1, 70000, 150_000.0, 1),
    ]
    raw_total = client.query(
        f"SELECT sum(bytes * ((input_if != 0) + (output_if != 0))) FROM {db}.flow_records "
        "WHERE exporter_ip = '10.77.0.1'").result_rows[0][0]
    assert sum(r[3] for r in got) == pytest.approx(raw_total)


@live
def test_ipv6_conversations_stay_distinct_and_the_guard_excludes_nsel_create_and_deny(databases):
    db = databases(*RELEASE_PATH)
    client = _connect()
    rows = [r for r in _generate_rows(BASE, count=800, seed=3)]
    _insert(client, db, rows)
    client.command(f"OPTIMIZE TABLE {db}.flow_conversations_1h FINAL")
    v6 = client.query(
        f"SELECT count(), uniqExact(src_addr6, dst_addr6, exporter_ip6, exporter_ip, protocol, dst_port, timestamp) "
        f"FROM {db}.flow_conversations_1h WHERE src_addr6 != toIPv6('::')").result_rows[0]
    assert v6[0] > 1 and v6[0] == v6[1]
    raw = client.query(
        f"SELECT countIf({nr.VOLUME_GUARD}), countIf(fw_event IN (1, 3)) FROM {db}.flow_records").result_rows[0]
    assert raw[1] > 0
    for table, column in (("flow_conversations_1h", "flows"), ("flow_qos_1h", "flows"),
                          ("flow_traffic_5m", "flow_count"), ("flow_exporter_minutes", "records")):
        assert client.query(f"SELECT sum({column}) FROM {db}.{table}").result_rows[0][0] == raw[0], table
    if_sides = client.query(
        f"SELECT sumIf(toUInt64(input_if != 0) + toUInt64(output_if != 0), {nr.VOLUME_GUARD}) "
        f"FROM {db}.flow_records").result_rows[0][0]
    assert client.query(f"SELECT sum(flows) FROM {db}.flow_interface_5m").result_rows[0][0] == if_sides


@live
def test_fresh_install_converges_to_the_upgraded_schema(databases):
    upgraded = databases(*RELEASE_PATH)
    # install.sh: init, then every migrate-*-clickhouse.sql in lexical order.
    fresh = databases("init-clickhouse.sql", *sorted(RELEASE_PATH))
    client = _connect()
    names = [r[0] for r in client.query(
        f"SELECT name FROM system.tables WHERE database = '{upgraded}' ORDER BY name").result_rows]
    assert names == [r[0] for r in client.query(
        f"SELECT name FROM system.tables WHERE database = '{fresh}' AND name LIKE 'flow%' ORDER BY name").result_rows]
    for name in names:
        a = client.command(f"SHOW CREATE TABLE {upgraded}.{name}").replace(upgraded, "DB")
        b = client.command(f"SHOW CREATE TABLE {fresh}.{name}").replace(fresh, "DB")
        assert a == b, name


@live
def test_migration_applies_twice(databases):
    db = databases(*RELEASE_PATH)
    client = _connect()
    _apply(client, db, MIGRATION)
    assert client.query(
        f"SELECT count() FROM system.tables WHERE database = '{db}' AND engine = 'MaterializedView'"
    ).result_rows[0][0] == 5


# ─── Live: full sweeps (X-02 repair, X-11 floor) with a stand-in Postgres ────


class _Result:
    def __init__(self, first=None, scalar=None):
        self._first, self._scalar = first, scalar

    def first(self):
        return self._first

    def scalar(self):
        return self._scalar


class _Session:
    """Postgres stand-in: the advisory lock always succeeds; system_settings is a dict."""

    store: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        sql = str(stmt).strip()
        params = params or {}
        if "pg_try_advisory_xact_lock" in sql:
            return _Result(scalar=True)
        if sql.startswith("SELECT value FROM system_settings"):
            value = self.store.get(params.get("k") or params.get("key"))
            return _Result(first=(value,) if value is not None else None)
        if sql.startswith("INSERT INTO system_settings"):
            self.store[params["k"]] = json.loads(params["v"])
            return _Result()
        raise AssertionError(sql)

    async def commit(self):
        return None


@pytest.fixture
def fake_pg(monkeypatch):
    _Session.store = {}
    monkeypatch.setattr(nr, "AsyncSessionLocal", _Session)
    monkeypatch.setattr(nr, "BACKFILL_PAUSE_S", 0.0)
    monkeypatch.setattr(nr, "HEAL_PAUSE_S", 0.0)
    return _Session.store


def _wait_for_mutations(client, database: str) -> None:
    for _ in range(200):
        if not client.query(
            f"SELECT count() FROM system.mutations WHERE database = '{database}' AND NOT is_done"
        ).result_rows[0][0]:
            return
        time.sleep(0.1)
    raise AssertionError("mutation did not finish")


def _sweep_until_idle(t: nr.ChTarget, client, rounds: int = 8) -> None:
    for _ in range(rounds):
        delay = asyncio.run(nr.sweep_once(t))
        if delay == nr.MUTATION_RECHECK_S:
            _wait_for_mutations(client, t.database)
            continue
        return
    raise AssertionError("healer never went idle")


def _rollup_bytes_by_hour(client, db: str, table: str) -> dict[int, int]:
    return dict(client.query(
        f"SELECT toUnixTimestamp(toStartOfHour(timestamp)) h, sum(bytes) FROM {db}.{table} GROUP BY h"
    ).result_rows)


def _raw_bytes_by_hour(client, db: str) -> dict[int, int]:
    return dict(client.query(
        f"SELECT toUnixTimestamp(toStartOfHour(timestamp)) h, sumIf(bytes, {nr.VOLUME_GUARD}) "
        f"FROM {db}.flow_records GROUP BY h"
    ).result_rows)


def _oldest_raw_hour(client, db: str) -> int:
    return client.query(
        f"SELECT toUnixTimestamp(toStartOfHour(min(timestamp))) FROM {db}.flow_records").result_rows[0][0]


def _v5_rows(base: datetime, hours: int, per_hour: int, header: int) -> list[tuple]:
    """Sampled v5 rows as the pre-v2 collector stored them: the raw header word
    as sampling_interval and counters multiplied by it."""
    rows = []
    for h in range(hours):
        for k in range(per_hour):
            # One-second flows well inside their hour: nothing spreads into
            # the hour before, which may sit below the healer's floor.
            end = base + timedelta(hours=h, minutes=1 + k % 58, seconds=k % 7)
            rows.append((end, end + timedelta(seconds=1), "t", "10.55.0.1", 5, header,
                         f"10.2.0.{k % 5 + 1}", "10.3.0.1", 1, 2, 3 * header, 1500 * header,
                         1000, 2000, 40000 + k % 3, 443, 18, 6, 0))
    return rows


@live
def test_x02_repair_and_backfill_end_to_end(databases, fake_pg):
    """Pre-v2 appliance: inflated v5 rows in raw and in the v1 rollups. After
    migrate-137 and the healer, raw is corrected, the old rollups hold the
    corrected bytes, the new rollups are back-filled, and a second sweep does
    nothing."""
    client = _connect()
    db = databases(*UPGRADE_PATH[:-1])
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=12)
    unsampled = [r[:len(V1_COLUMNS)] for r in _generate_rows(start - timedelta(hours=2), hours=8, count=400)
                 if r[4] != 5 and r[RAW_COLUMNS.index("fw_event")] == 0 and r[3] != "0.0.0.0"
                 and r[RAW_COLUMNS.index("ip_version")] == 4]
    _insert(client, db, unsampled, columns=V1_COLUMNS, batches=2)
    _insert(client, db, _v5_rows(start, 5, 30, 0x4064), columns=V1_COLUMNS, batches=2)
    inflated = sum(_rollup_bytes_by_hour(client, db, "flow_conversations_1h").values())

    _apply(client, db, MIGRATION, DEDUP_MIGRATION)
    t = _target(db)
    _sweep_until_idle(t, client)

    assert client.query(f"SELECT count() FROM {db}.flow_records WHERE {nr.X02_PREDICATE}").result_rows[0][0] == 0
    assert client.query(
        f"SELECT min(sampling_interval), max(sampling_interval), sum(bytes) FROM {db}.flow_records "
        "WHERE exporter_ip = '10.55.0.1'").result_rows[0] == (100, 100, 5 * 30 * 1500 * 100)
    # Hours from the healer's floor (the oldest raw hour may be partial, so
    # it is never rebuilt) up to the last settled hour.
    floor = _oldest_raw_hour(client, db) + H
    raw = {h: b for h, b in _raw_bytes_by_hour(client, db).items()
           if h >= floor and h + 2 * H <= now.timestamp()}
    assert len(raw) >= 6
    for table in ("flow_conversations_1h", "flow_qos_1h", "flow_traffic_5m"):
        rolled = _rollup_bytes_by_hour(client, db, table)
        assert {h: rolled.get(h) for h in raw} == raw, table
    assert sum(_rollup_bytes_by_hour(client, db, "flow_conversations_1h").values()) < inflated
    window = f"timestamp >= toDateTime({floor}, 'UTC') AND timestamp < toDateTime({int(now.timestamp()) - 3 * H}, 'UTC')"
    sides = client.query(
        f"SELECT sumIf(toUInt64(input_if != 0) + toUInt64(output_if != 0), {nr.VOLUME_GUARD}) "
        f"FROM {db}.flow_records WHERE {window}").result_rows[0][0]
    assert sides > 0
    assert client.query(f"SELECT sum(flows) FROM {db}.flow_interface_5m WHERE {window}").result_rows[0][0] == sides
    assert client.query(f"SELECT sum(records) FROM {db}.flow_exporter_minutes").result_rows[0][0] > 0

    x02 = fake_pg[nr.REPAIR_SETTINGS_KEY]["x02"]
    assert x02["reconciled_at"] and x02["queue"] == {}
    assert x02["rebuilt"].get("flow_conversations_1h", 0) >= 5
    assert x02["rebuilt"].get("flow_traffic_5m", 0) >= 5

    # Idle: nothing left to plan, so no rollup is rewritten.
    mutations = client.query(
        f"SELECT count() FROM system.mutations WHERE database = '{db}'").result_rows[0][0]
    _sweep_until_idle(t, client)
    assert client.query(
        f"SELECT count() FROM system.mutations WHERE database = '{db}'").result_rows[0][0] == mutations


@live
def test_unmasked_rows_arriving_after_the_upgrade_are_corrected_everywhere(databases, fake_pg):
    client = _connect()
    db = databases(*RELEASE_PATH)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=10)
    _insert(client, db, _v5_rows(start - timedelta(hours=1), 1, 5, 100), columns=V1_COLUMNS, batches=1)
    t = _target(db)
    _sweep_until_idle(t, client)
    # A pre-v2 collector keeps writing after migrate-137 ran.
    _insert(client, db, _v5_rows(start, 3, 20, 0x83E8), columns=V1_COLUMNS, batches=1)
    _sweep_until_idle(t, client)

    assert client.query(f"SELECT count() FROM {db}.flow_records WHERE {nr.X02_PREDICATE}").result_rows[0][0] == 0
    raw = _raw_bytes_by_hour(client, db)
    for table in ("flow_conversations_1h", "flow_qos_1h", "flow_traffic_5m"):
        assert _rollup_bytes_by_hour(client, db, table) == raw, table
    iface = client.query(f"SELECT sum(bytes) FROM {db}.flow_interface_5m").result_rows[0][0]
    raw_iface = client.query(
        f"SELECT sum(bytes * ((input_if != 0) + (output_if != 0))) FROM {db}.flow_records").result_rows[0][0]
    assert iface == pytest.approx(raw_iface)
    assert client.query(f"SELECT sum(bytes) FROM {db}.flow_exporter_minutes").result_rows[0][0] == sum(raw.values())
    assert fake_pg[nr.REPAIR_SETTINGS_KEY]["x02"]["rows_found_after_upgrade"] == 60


@live
def test_rollup_history_at_the_raw_ttl_edge_is_left_alone(databases, fake_pg):
    """X-11: raw TTL lowered to 5 days; the hour straddling the cutoff lost raw
    rows to TTL but the rollup still holds all of them."""
    client = _connect()
    db = databases(*RELEASE_PATH)
    client.command(f"ALTER TABLE {db}.flow_records MODIFY TTL toDateTime(timestamp) + toIntervalDay(5)")
    cutoff = datetime.now(timezone.utc) - timedelta(days=5)
    first = cutoff - timedelta(minutes=30)
    rows = [(first + timedelta(minutes=k), first + timedelta(minutes=k, seconds=1), "t", "10.66.0.1",
             9, 1, "10.4.0.1", "10.5.0.1", 1, 2, 1, 1000, 1000, 2000, 40000, 443, 16, 6, 0) for k in range(60)]
    _insert(client, db, rows, columns=V1_COLUMNS, batches=1)
    client.command(f"OPTIMIZE TABLE {db}.flow_records FINAL")
    raw_kept = client.query(f"SELECT count() FROM {db}.flow_records").result_rows[0][0]
    assert raw_kept < 60
    before = client.query(f"SELECT sum(flows) FROM {db}.flow_conversations_1h").result_rows[0][0]
    assert before == 60
    _sweep_until_idle(_target(db), client)
    assert client.query(f"SELECT sum(flows) FROM {db}.flow_conversations_1h").result_rows[0][0] == 60
