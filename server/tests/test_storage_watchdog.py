"""Free-space watchdog: pressure policy, bounded purging, pending reclamation.

ClickHouse and the filesystem are simulated. Dropped partitions only return
their space after a delay, as with ClickHouse's background part removal, so
the tests catch over-deletion caused by re-checking usage too early.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import storage_policy as P

ROOT = Path(__file__).resolve().parents[2]
GIB = P.GIB
TODAY = date(2026, 10, 10)
NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc).timestamp()


# ─── Policy unit tests ───────────────────────────────────────────────────────

def fs(used_gib, avail_gib, total_gib=None, mount="/", device=1):
    total = total_gib if total_gib is not None else used_gib + avail_gib + 5
    return P.FsSample(mount, device, int(total * GIB), int(used_gib * GIB), int(avail_gib * GIB))


def test_usage_excludes_root_reserved_blocks():
    s = fs(75, 25, total_gib=105)
    assert s.capacity == 100 * GIB and s.reserved_bytes == 5 * GIB and s.usage_pct == 75.0


def test_shared_root_and_data_are_one_filesystem():
    merged = P.dedupe_filesystems([fs(50, 50, mount="/"), fs(50, 50, mount="/data")])
    assert len(merged) == 1 and merged[0].mount == "/ /data"


def test_pressure_targets_the_configured_level():
    p = P.evaluate_pressure(fs(90, 10), P.DEFAULT_POLICY, 0, None)
    assert p.state == "pressure" and p.need_bytes == 15 * GIB
    assert "application" in p.tiers


def test_pending_reclamation_prevents_repeat_deletion():
    p = P.evaluate_pressure(fs(90, 10), P.DEFAULT_POLICY, 15 * GIB, None)
    assert p.state == "ok" and p.need_bytes == 0


def test_low_free_bytes_trigger_even_at_low_percentage():
    big = fs(950, 4, total_gib=1000)        # 99.6%... use a huge disk at lower %:
    huge = fs(3000, 4, total_gib=3100)
    p = P.evaluate_pressure(huge, P.DEFAULT_POLICY, 0, None)
    assert p.need_bytes >= int(5 * GIB * 1.5) - 4 * GIB
    assert big.usage_pct > 99


def test_fast_growth_sheds_diagnostics_only():
    rate = 8 * GIB / 3600          # 8 GiB/h with 40 GiB free → full in 5 h
    p = P.evaluate_pressure(fs(60, 40), P.DEFAULT_POLICY, 0, rate)
    assert p.state == "growth" and p.tiers == ("legacy", "diagnostic")
    assert round(p.runway_hours) == 5


def test_critical_without_need_keeps_state_critical():
    p = P.evaluate_pressure(fs(99, 1.5), P.DEFAULT_POLICY, 40 * GIB, None)
    assert p.state == "critical" and p.need_bytes == 0


def test_policy_is_clamped_and_garbage_falls_back():
    p = P.normalize_policy({"threshold_pct": 120, "target_pct": 99, "min_keep_days": 0})
    assert p["threshold_pct"] == 98 and p["target_pct"] == 97 and p["min_keep_days"] == 1
    assert P.normalize_policy({"threshold_pct": "x"}) == P.DEFAULT_POLICY


@pytest.mark.parametrize("table,expected", [
    ("text_log", ("text_log", False)),
    ("text_log_0", ("text_log", True)),
    ("query_log_pre_recovery_20261007", ("query_log", True)),
    ("part_log_pre_recovery_20261007_1", ("part_log", True)),
    ("error_log", ("error_log", False)),
    ("parts", None),
    ("tables", None),
])
def test_diagnostic_log_classification(table, expected):
    assert P.diagnostic_log(table) == expected


def part(db, table, pid, gib, newest):
    return {"database": db, "table": table, "partition_id": pid, "bytes": int(gib * GIB),
            "newest": newest}


PARTITIONS = [
    part("system", "text_log_0", "202610", 9, TODAY),                   # legacy copy
    part("system", "query_log_pre_recovery_20261007", "202610", 0.7, TODAY - timedelta(days=3)),
    part("system", "text_log", "20261008", 2, TODAY - timedelta(days=2)),
    part("system", "text_log", "20261009", 2, TODAY - timedelta(days=1)),
    part("system", "text_log", "20261010", 1, TODAY),                    # today: never
    part("system", "error_log", "20261001", 0.1, TODAY - timedelta(days=9)),  # protected
    part("zenplus", "flow_records", "20260901", 4, date(2026, 9, 1)),
    part("zenplus", "snmp_metrics", "202609", 3, date(2026, 9, 30)),
    part("zenplus", "snmp_metrics", "202610", 3, TODAY),                 # inside keep window
    part("zenplus", "flow_templates", "202601", 1, date(2026, 1, 1)),    # state table
    part("zenplus", "snmp_metrics_1h", "202608", 2, date(2026, 8, 31)),
]


def test_candidates_are_ordered_cheapest_to_lose_first():
    cands = P.build_candidates(PARTITIONS, TODAY, P.DEFAULT_POLICY)
    order = [(c.tier, c.table, c.partition_id) for c in cands]
    assert order[0][0] == "legacy" and order[1][0] == "legacy"
    diag = [o for o in order if o[0] == "diagnostic"]
    assert diag == [("diagnostic", "text_log", "20261008"), ("diagnostic", "text_log", "20261009")]
    assert ("diagnostic_truncate", "text_log", None) in order
    app = [o[1:] for o in order if o[0] == "application"]
    # Least valuable category first, then oldest; data inside the keep window,
    # state tables and protected logs never.
    assert app == [("flow_records", "20260901"), ("snmp_metrics", "202609"), ("snmp_metrics_1h", "202608")]
    assert not any(o[1] in ("error_log", "flow_templates") for o in order)
    assert not any(o[2] in ("20261010", "202610") and o[1] in ("text_log", "snmp_metrics") for o in order)


def test_truncate_counts_only_what_partition_drops_left():
    cands = P.build_candidates(PARTITIONS, TODAY, P.DEFAULT_POLICY)
    pressure = P.Pressure("pressure", 14 * GIB, ("legacy", "diagnostic", "diagnostic_truncate"))
    plan = P.plan_pass(cands, pressure)
    truncate = [c for c in plan if c.tier == "diagnostic_truncate"]
    assert [(c.table, c.bytes) for c in truncate] == [("text_log", GIB)]


def test_plan_stops_once_the_need_is_covered():
    cands = P.build_candidates(PARTITIONS, TODAY, P.DEFAULT_POLICY)
    pressure = P.Pressure("pressure", 10 * GIB, ("legacy", "diagnostic", "diagnostic_truncate", "application"))
    plan = P.plan_pass(cands, pressure)
    # Legacy copies oldest first, then the oldest diagnostic day.
    assert [c.table for c in plan] == ["query_log_pre_recovery_20261007", "text_log_0", "text_log"]
    growth = P.Pressure("growth", 100 * GIB, ("legacy", "diagnostic"))
    assert all(c.tier in ("legacy", "diagnostic") for c in P.plan_pass(cands, growth))


def test_candidate_sql_is_identifier_safe():
    assert P.Candidate("diagnostic", "system", "text_log", "20261008", 1, None).sql() == \
        "ALTER TABLE system.text_log DROP PARTITION ID '20261008'"
    assert P.Candidate("legacy", "system", "text_log_0", None, 1, None).sql() == \
        "DROP TABLE IF EXISTS system.text_log_0 SYNC"
    with pytest.raises(ValueError):
        P.Candidate("diagnostic", "system", "t; DROP", "1", 1, None).sql()
    with pytest.raises(ValueError):
        P.Candidate("application", "zenplus", "t", "1' OR '1", 1, None).sql()


def test_legacy_copies_expire_with_their_log_retention():
    cands = P.build_candidates(PARTITIONS, TODAY, P.DEFAULT_POLICY)
    expired = [c.table for c in P.legacy_expired(cands, TODAY)]
    # text_log_0 still has today's data; the query_log copy is 3 days old (< 7).
    assert expired == []
    later = TODAY + timedelta(days=8)
    assert {c.table for c in P.legacy_expired(cands, later)} == {
        "text_log_0", "query_log_pre_recovery_20261007"}


def test_growth_rate_needs_a_real_window():
    assert P.growth_rate([(0, 0), (60, 10)]) is None
    assert P.growth_rate([(0, 0), (600, 600), (900, 900)]) == pytest.approx(1.0)


def test_logging_config_matches_policy_retention():
    root = ET.parse(ROOT / "docker" / "clickhouse" / "zenplus_logging.xml").getroot()
    assert root.find("logger/level").text == "warning"
    assert root.find("text_log/level").text == "warning"
    configured = {}
    for el in root:
        if el.tag.endswith("_log"):
            assert el.findtext("partition_by") == "event_date", el.tag
            ttl = el.findtext("ttl")
            days = int(ttl.split("INTERVAL")[1].split("DAY")[0])
            configured[el.tag] = days
    for name, days in configured.items():
        assert P.DIAGNOSTIC_LOG_RETENTION_DAYS[name] == days, name
    # Logs in the policy but not overridden keep the image's own 3-day TTL.
    assert set(P.DIAGNOSTIC_LOG_RETENTION_DAYS) - set(configured) == {"asynchronous_insert_log"}
    profile = ET.parse(ROOT / "docker" / "clickhouse" / "zenplus_profile.xml").getroot()
    default = profile.find("profiles/default")
    assert default.findtext("log_processors_profiles") == "0"
    assert default.findtext("log_query_views") == "0"
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "config.d/zenplus_logging.xml:ro" in compose and "users.d/zenplus_profile.xml:ro" in compose


# ─── Watchdog ────────────────────────────────────────────────────────────────

class FakeDisk:
    """A filesystem whose dropped bytes return after ``delay_s`` (like ClickHouse)."""

    def __init__(self, used_gib, avail_gib, delay_s=600):
        self.used = int(used_gib * GIB)
        self.avail = int(avail_gib * GIB)
        self.delay_s = delay_s
        self.releases: list[tuple[float, int]] = []

    def free(self, now, size, immediate):
        self.releases.append((now if immediate else now + self.delay_s, size))

    def sample(self, now):
        for at, size in list(self.releases):
            if now >= at:
                self.used -= size
                self.avail += size
                self.releases.remove((at, size))
        return [P.FsSample("/", 1, self.used + self.avail + 5 * GIB, self.used, self.avail),
                P.FsSample("/data", 1, self.used + self.avail + 5 * GIB, self.used, self.avail)]


class FakeClickHouse:
    def __init__(self, partitions, disk, clock):
        self.partitions = [dict(p) for p in partitions]
        self.disk = disk
        self.clock = clock
        self.commands: list[str] = []
        self.down = False

    class _R:
        def __init__(self, rows):
            self.result_rows = rows

    def query(self, sql, parameters=None):
        if self.down:
            raise ConnectionError("Connection refused")
        if "detached_parts" in sql:
            return self._R([[0]])
        if "GROUP BY database, table, active" in sql:
            return self._R([[p["database"], p["table"], 1, p["bytes"], 1] for p in self.partitions])
        return self._R([[p["database"], p["table"], p["partition_id"], p["bytes"],
                         p["newest"], None] for p in self.partitions])

    def command(self, sql):
        if self.down:
            raise ConnectionError("Connection refused")
        self.commands.append(sql)
        name = sql.split("system.")[-1].split("zenplus.")[-1].split()[0]
        immediate = "SYNC" in sql
        if "DROP PARTITION" in sql:
            pid = sql.split("'")[1]
            gone = [p for p in self.partitions if p["table"] == name and p["partition_id"] == pid]
        else:
            gone = [p for p in self.partitions if p["table"] == name]
        for p in gone:
            self.partitions.remove(p)
            self.disk.free(self.clock(), p["bytes"], immediate)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.services import storage_watchdog as W

    monkeypatch.setattr(W, "STATE_DIR", str(tmp_path))
    clock = {"t": NOW}
    disk = FakeDisk(90, 10)
    ch = FakeClickHouse(PARTITIONS, disk, lambda: clock["t"])

    def make(ch_device=1, sampler=None):
        return W.Watchdog(sampler=sampler or (lambda: disk.sample(clock["t"])),
                          client_factory=lambda: ch, clock=lambda: clock["t"],
                          ch_device=lambda: ch_device)
    return W, make, clock, disk, ch, tmp_path


def audit(path):
    return [json.loads(l) for l in (path / "audit.jsonl").read_text().splitlines()]


def test_pressure_pass_is_bounded_and_does_not_repeat_while_reclaim_is_pending(env):
    W, make, clock, disk, ch, tmp = env
    dog = make()
    state = dog.tick()
    # Needs 15 GiB: legacy text_log_0 (9, immediate) + query copy (0.7, immediate)
    # + oldest text_log days (2 + 2, delayed) + truncate text_log (rest).
    assert set(ch.commands[:2]) == {"DROP TABLE IF EXISTS system.text_log_0 SYNC",
                                    "DROP TABLE IF EXISTS system.query_log_pre_recovery_20261007 SYNC"}
    assert len([a for a in audit(tmp) if a["type"] == "watchdog_purge"]) == 1
    # Diagnostics (14.7 GiB) fall just short of the 15 GiB need: only then is the
    # least valuable, oldest monitoring data (raw flows) used.
    app = [c for c in ch.commands if "zenplus." in c]
    assert app == ["ALTER TABLE zenplus.flow_records DROP PARTITION ID '20260901'"]
    assert not any("error_log" in c for c in ch.commands)
    first = len(ch.commands)
    record = [a for a in audit(tmp) if a["type"] == "watchdog_purge"][0]
    assert record["details"]["scheduled_bytes"] >= 15 * GIB - 1
    assert state["pending_bytes"] > 0

    # 30 s later the delayed partitions have not been removed yet: no new deletions.
    clock["t"] += 30
    dog.tick()
    assert len(ch.commands) == first

    # After the reclamation window the space is back and the outcome is measured.
    clock["t"] += 20 * 60
    state = dog.tick()
    assert len(ch.commands) == first
    reclaim = [a for a in audit(tmp) if a["type"] == "watchdog_reclaim"]
    d = reclaim[0]["details"]
    # Scheduled bytes are what was really freed: no double counting.
    assert d["measured_free_change_bytes"] == d["scheduled_bytes"] >= 15 * GIB - 1
    assert state["state"] == "ok" and state["filesystems"][0]["mount"] == "/ /data"


def test_nothing_eligible_pauses_ingest_and_resumes_with_margin(env):
    W, make, clock, disk, ch, tmp = env
    ch.partitions = [p for p in ch.partitions if p["table"] in ("error_log", "flow_templates")
                     or p["partition_id"] in ("202610", "20261010")]
    ch.partitions = [p for p in ch.partitions if p["database"] != "system" or p["table"] == "error_log"]
    disk.used, disk.avail = int(98.5 * GIB), int(1.5 * GIB)
    dog = make()
    state = dog.tick()
    assert state["ingest_paused"] is True and (tmp / "ingest-paused").exists()
    assert state["filesystems"][0]["exhausted"] is True
    types = [a["type"] for a in audit(tmp)]
    assert types.count("watchdog_exhausted") == 1 and "watchdog_ingest_paused" in types

    clock["t"] += 30
    dog.tick()
    assert [a["type"] for a in audit(tmp)].count("watchdog_exhausted") == 1   # rate-limited

    # Space freed elsewhere, but only just above critical: stay paused.
    disk.used, disk.avail = int(97 * GIB), int(3 * GIB)
    clock["t"] += 30
    assert make().tick()["ingest_paused"] is True
    # Comfortably back: resume.
    disk.used, disk.avail = int(70 * GIB), int(30 * GIB)
    clock["t"] += 30
    state = make().tick()
    assert state["ingest_paused"] is False and not (tmp / "ingest-paused").exists()
    assert "watchdog_ingest_resumed" in [a["type"] for a in audit(tmp)]


def test_clickhouse_outage_is_survived_and_reported(env):
    W, make, clock, disk, ch, tmp = env
    ch.down = True
    disk.used, disk.avail = int(98.8 * GIB), int(1.2 * GIB)
    state = make().tick()
    assert state["last_error"]["message"].startswith("ClickHouse")
    assert state["ingest_paused"] is True
    assert state["heartbeat"] == NOW


def test_disabled_policy_never_deletes(env):
    W, make, clock, disk, ch, tmp = env
    (tmp / "policy.json").write_text(json.dumps({"policy": {"enabled": False}}))
    make().tick()
    assert ch.commands == []


def test_garbage_policy_file_falls_back_to_defaults(env):
    W, make, clock, disk, ch, tmp = env
    (tmp / "policy.json").write_text("{not json")
    assert make().policy() == P.DEFAULT_POLICY


def test_legacy_sweep_runs_without_pressure(env):
    W, make, clock, disk, ch, tmp = env
    disk.used, disk.avail = int(20 * GIB), int(80 * GIB)
    clock["t"] += 9 * 86400           # both legacy copies now past retention
    make().tick()
    assert "DROP TABLE IF EXISTS system.text_log_0 SYNC" in ch.commands
    assert not any("ALTER TABLE" in c for c in ch.commands)


def test_state_survives_restart(env):
    W, make, clock, disk, ch, tmp = env
    make().tick()
    pending = json.loads((tmp / "state.json").read_text())["pending_bytes"]
    clock["t"] += 30
    dog = make()                      # restarted process
    before = len(ch.commands)
    assert dog.tick()["pending_bytes"] == pending
    assert len(ch.commands) == before


def test_watchdog_does_not_import_the_database_layers():
    code = ("import sys; import app.services.storage_watchdog; "
            "bad=[m for m in ('sqlalchemy','asyncpg','app.core.database','app.core.config') if m in sys.modules]; "
            "print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT / "server", capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_single_instance_lock(env, monkeypatch):
    W, make, clock, disk, ch, tmp = env
    import fcntl
    holder = open(tmp / "watchdog.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert W.main() == 1
    holder.close()


def test_audit_trail_is_bounded(env, monkeypatch):
    W, make, clock, disk, ch, tmp = env
    monkeypatch.setattr(W, "AUDIT_MAX_BYTES", 2000)
    for i in range(200):
        W.append_audit("watchdog_error", {"message": "x" * 50, "i": i}, NOW)
    assert (tmp / "audit.jsonl").stat().st_size <= 2200
    assert (tmp / "audit.jsonl.1").exists()
    assert W.read_audit(5)[-1]["details"]["i"] == 199


def test_monitoring_history_is_spared_when_it_cannot_fix_a_shared_disk(env):
    W, make, clock, disk, ch, tmp = env
    # 88% full of other files; everything purgeable totals far less than needed.
    ch.partitions = [part("system", "text_log", "20261008", 0.2, TODAY - timedelta(days=2)),
                     part("zenplus", "flow_records", "20260901", 0.5, date(2026, 9, 1))]
    disk.used, disk.avail = int(88 * GIB), int(12 * GIB)
    make().tick()
    assert ch.commands == ["ALTER TABLE system.text_log DROP PARTITION ID '20261008'"]
    assert "watchdog_application_spared" in [a["type"] for a in audit(tmp)]


def test_monitoring_history_is_last_resort_above_95_percent(env):
    W, make, clock, disk, ch, tmp = env
    ch.partitions = [part("system", "text_log", "20261008", 0.2, TODAY - timedelta(days=2)),
                     part("zenplus", "flow_records", "20260901", 0.5, date(2026, 9, 1))]
    disk.used, disk.avail = int(96 * GIB), int(4 * GIB)
    make().tick()
    assert "ALTER TABLE zenplus.flow_records DROP PARTITION ID '20260901'" in ch.commands


def test_full_separate_root_never_costs_monitoring_data(env):
    W, make, clock, disk, ch, tmp = env
    # / is its own filesystem (device 2), full of OS files; ClickHouse lives on /data (device 1).
    root = P.FsSample("/", 2, 55 * GIB, 49 * GIB, int(0.5 * GIB))
    data = P.FsSample("/data", 1, 105 * GIB, 20 * GIB, 80 * GIB)
    dog = make(ch_device=1, sampler=lambda: [root, data])
    for _ in range(4):
        state = dog.tick()
        clock["t"] += 20 * 60
    assert not any("ALTER TABLE zenplus" in c for c in ch.commands)
    assert state["ingest_paused"] is False          # pausing telemetry cannot help /
    root_eval = next(f for f in state["filesystems"] if f["mount"] == "/")
    assert root_eval["exhausted"] is True and root_eval["holds_clickhouse"] is False


def test_pause_marker_survives_a_full_disk(env, monkeypatch):
    W, make, clock, disk, ch, tmp = env
    real_open = open

    def enospc_on_marker(path, mode="r", *a, **k):
        if str(path).endswith("ingest-paused") and "w" in mode:
            raise OSError(28, "No space left on device")
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr("builtins.open", enospc_on_marker)
    ch.down = True
    disk.used, disk.avail = int(99 * GIB), int(1 * GIB)
    state = make().tick()
    assert (tmp / "ingest-paused").exists()          # empty-file fallback
    assert state["ingest_paused"] is True


def test_growth_need_accounts_for_pending_reclamation():
    rate = 8 * GIB / 3600
    p = P.evaluate_pressure(fs(60, 40), P.DEFAULT_POLICY, 60 * GIB, rate)
    # With 60 GiB about to return the runway is long enough: no more shedding.
    assert p.state == "ok"
    p = P.evaluate_pressure(fs(60, 30), P.DEFAULT_POLICY, 5 * GIB, rate)
    assert p.state == "growth" and p.need_bytes == int(rate * 6 * 3600) - 5 * GIB
