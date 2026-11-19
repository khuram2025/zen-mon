"""System status: the appliance's own CPU, memory, services and history.

Nothing here asks a real systemd, database or Redis: unit lists, probes and the
sample store are passed in.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import appliance_health as H

STAT_A = "cpu  1000 0 500 8000 400 0 50 50 0 0\ncpu0 1 2 3 4 5 6 7 8\nintr 1\n"
STAT_B = "cpu  1300 0 600 8500 500 0 50 50 0 0\n"

MEMINFO = """MemTotal:       16000000 kB
MemFree:         2000000 kB
MemAvailable:    4000000 kB
Cached:          3000000 kB
SwapTotal:       4000000 kB
SwapFree:        3000000 kB
"""


def test_cpu_usage_is_the_busy_share_of_the_interval():
    usage = H.cpu_usage(H.parse_cpu_times(STAT_A), H.parse_cpu_times(STAT_B))
    # 1000 jiffies passed: 500 idle, 100 waiting for disk, 400 busy.
    assert usage == {"usage_pct": 40.0, "iowait_pct": 10.0, "steal_pct": 0.0}


def test_cpu_usage_is_unavailable_not_zero_without_two_readings():
    a = H.parse_cpu_times(STAT_A)
    assert H.cpu_usage(None, a) is None
    assert H.cpu_usage(a, a) is None                      # counters did not advance
    assert H.cpu_usage(H.parse_cpu_times(STAT_B), a) is None   # went backwards (reboot)
    assert H.parse_cpu_times("intr 5\n") is None
    assert H.parse_cpu_times("cpu  a b c d\n") is None


def test_memory_counts_available_not_free_and_reports_swap():
    m = H.parse_meminfo(MEMINFO)
    assert m["total_bytes"] == 16000000 * 1024
    assert m["used_bytes"] == 12000000 * 1024   # page cache is not "used"
    assert m["used_pct"] == 75.0
    assert m["swap_used_bytes"] == 1000000 * 1024 and m["swap_used_pct"] == 25.0


def test_missing_memory_lines_are_unavailable():
    m = H.parse_meminfo("MemTotal: 1000 kB\n")
    assert m["total_bytes"] == 1024000 and m["used_bytes"] is None and m["used_pct"] is None
    assert m["swap_total_bytes"] is None and m["swap_used_pct"] is None
    # No swap configured: 0 of 0 is a fact; a percentage of nothing is not.
    none = H.parse_meminfo("MemTotal: 10 kB\nMemAvailable: 5 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    assert none["swap_total_bytes"] == 0 and none["swap_used_bytes"] == 0 and none["swap_used_pct"] is None


SHOW = """NRestarts=2
MemoryCurrent=1448898560
CPUUsageNSec=128384334000
Id=zenplus-api.service
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled
ActiveEnterTimestampMonotonic=22192377

NRestarts=0
MemoryCurrent=[not set]
CPUUsageNSec=[not set]
Id=zenplus-poller.service
LoadState=loaded
ActiveState=failed
SubState=failed
UnitFileState=enabled
ActiveEnterTimestampMonotonic=0

NRestarts=0
MemoryCurrent=736366592
CPUUsageNSec=56919929000
Id=postgresql@16-main.service
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled-runtime
ActiveEnterTimestampMonotonic=16399909

NRestarts=0
MemoryCurrent=[not set]
CPUUsageNSec=[not set]
Id=zenplus-netflow-collector.service
LoadState=loaded
ActiveState=inactive
SubState=dead
UnitFileState=disabled
ActiveEnterTimestampMonotonic=0

NRestarts=0
MemoryCurrent=[not set]
CPUUsageNSec=[not set]
Id=zenplus-syslog.service
LoadState=not-found
ActiveState=inactive
SubState=dead
UnitFileState=
ActiveEnterTimestampMonotonic=0
"""


def _spec(service_id: str) -> H.ServiceSpec:
    return next(s for s in H.SERVICES if s.id == service_id)


def test_unit_rows_carry_memory_cpu_restarts_and_running_time():
    units = H.parse_systemctl_show(SHOW)
    assert [u["Id"] for u in units][:2] == ["zenplus-api.service", "zenplus-poller.service"]
    api = H.service_row(_spec("api"), units, None, uptime_s=1000.0)
    assert api["state"] == "running" and api["memory_bytes"] == 1448898560
    assert api["cpu_seconds"] == 128.4 and api["restarts"] == 2
    assert api["uptime_s"] == 977       # entered the active state 22.2 s after boot


def test_a_failed_core_service_is_down_and_unset_accounting_is_unavailable():
    units = H.parse_systemctl_show(SHOW)
    poller = H.service_row(_spec("monitoring_engine"), units, None, uptime_s=1000.0)
    assert poller["state"] == "down" and poller["detail"] == "failed"
    assert poller["memory_bytes"] is None and poller["cpu_seconds"] is None and poller["uptime_s"] is None


def test_the_postgresql_unit_is_found_by_pattern_never_by_a_version_number():
    units = H.parse_systemctl_show(SHOW)
    ok = H.service_row(_spec("config_db"), units, {"ok": True}, 1000.0)
    assert ok["state"] == "running" and ok["memory_bytes"] == 736366592
    # The unit is active but the database does not answer: that is down.
    silent = H.service_row(_spec("config_db"), units, {"ok": False}, 1000.0)
    assert silent["state"] == "down" and silent["detail"] == "not answering"


def test_optional_services_switched_off_or_absent_are_not_faults():
    units = H.parse_systemctl_show(SHOW)
    assert H.service_row(_spec("flow_collector"), units, None, 1000.0)["state"] == "stopped"
    assert H.service_row(_spec("syslog_receiver"), units, None, 1000.0)["state"] == "not_installed"
    # A unit systemd did not list at all (pattern matched nothing).
    assert H.service_row(_spec("web_server"), units, None, 1000.0)["state"] == "not_installed"


def test_without_systemd_services_are_unknown_and_probes_still_decide():
    assert H.service_row(_spec("web_server"), None, None, None)["state"] == "unknown"
    assert H.service_row(_spec("monitoring_engine"), None, None, None)["state"] == "unknown"
    assert H.service_row(_spec("api"), None, None, None)["state"] == "running"
    row = H.service_row(_spec("metrics_db"), None, {"ok": True, "uptime_s": 90, "memory_bytes": 5}, None)
    assert (row["state"], row["uptime_s"], row["memory_bytes"]) == ("running", 90, 5)
    assert H.service_row(_spec("cache"), None, {"ok": False}, None)["state"] == "down"


def test_findings_name_what_is_wrong_and_stay_quiet_when_healthy():
    healthy = dict(services=[{"id": "api", "label": "Web application",
                              "core": True, "state": "running", "detail": None}],
                   memory={"used_pct": 40.0, "swap_used_pct": 0.0}, load=[0.5, 0.5, 0.5], cores=4,
                   ingestion={"monitoring": "ready", "uploads_paused": False},
                   clock={"ntp_synchronized": True})
    assert H.findings(**healthy) == []
    # Unknown readings never raise a finding.
    assert H.findings(**{**healthy, "memory": {"used_pct": None, "swap_used_pct": None}, "load": None,
                         "ingestion": {"monitoring": None, "uploads_paused": None},
                         "clock": {"ntp_synchronized": None}}) == []
    bad = H.findings(
        services=[{"id": "monitoring_engine", "label": "Monitoring engine", "core": True,
                   "state": "down", "detail": "failed"},
                  {"id": "flow_collector", "label": "Flow collector",
                   "core": False, "state": "down", "detail": "not running"},
                  {"id": "syslog_receiver", "label": "Syslog receiver",
                   "core": False, "state": "stopped", "detail": None}],
        memory={"used_pct": 93.0, "swap_used_pct": 60.0}, load=[9.0, 9.0, 9.0], cores=4,
        ingestion={"monitoring": "interrupted", "uploads_paused": True},
        clock={"ntp_synchronized": False}, cpu_15m=None, iowait_15m=40.0)
    codes = [f["code"] for f in bad]
    assert codes[:2] == ["service_down:monitoring_engine", "monitoring_interrupted"]
    assert [f["severity"] for f in bad[:2]] == ["critical", "critical"]
    assert set(codes[2:]) == {"service_down:flow_collector", "uploads_paused", "memory_high", "swap_high",
                              "load_high", "iowait_high", "clock_unsynchronized"}
    assert "service_down:syslog_receiver" not in codes
    assert bad[0]["message"] == "Monitoring engine is failed."
    # Sustained CPU replaces the load line instead of saying the same thing twice.
    both = H.findings(**{**healthy, "load": [9.0, 9.0, 9.0], "cpu_15m": 95.0})
    assert [f["code"] for f in both] == ["cpu_sustained"]
    assert H.findings(**{**healthy, "memory": {"used_pct": 98.0, "swap_used_pct": None}})[0]["code"] == "memory_critical"


def _sample(t, cpu=10.0, mem=50.0, disk_used=None):
    return {"t": t, "cpu": cpu, "iow": 1.0, "mem": mem, "swap": 0.0, "load": 0.5,
            "disk": 60.0, "disk_used": disk_used}


def test_history_buckets_keep_peaks_and_leave_gaps_empty():
    now = 100_000.0
    samples = [_sample(now - 3000, cpu=10), _sample(now - 2940, cpu=90), _sample(now - 300, cpu=20)]
    h = H.bucket_history(samples, now, hours=24, bucket_s=300)
    assert h["available"] and h["samples"] == 3
    first = h["points"][0]
    assert first["cpu"] == 50.0 and first["cpu_max"] == 90.0
    empty = [p for p in h["points"] if p["cpu"] is None]
    # Nothing was sampled between the first bucket and the last: those buckets are gaps, not zeros.
    assert len(empty) == len(h["points"]) - 2
    assert all(p["mem"] is None and p["load"] is None and p["disk"] is None for p in empty)
    assert [p["t"] for p in h["points"]] == sorted(p["t"] for p in h["points"])
    assert h["points"][-1]["t"] <= now


def test_history_without_samples_and_disk_change_over_the_window():
    assert H.bucket_history([], 1000.0)["points"] == []
    assert H.bucket_history([], 1000.0)["since"] is None
    now = 200_000.0
    grown = [_sample(now - 7200, disk_used=1000), _sample(now - 60, disk_used=4000)]
    assert H.bucket_history(grown, now)["disk_change"] == {"bytes": 3000, "hours": 2.0}
    # Less than an hour of readings says nothing about growth.
    short = [_sample(now - 600, disk_used=1000), _sample(now - 60, disk_used=4000)]
    assert H.bucket_history(short, now)["disk_change"] is None
    # Samples older than the window are not drawn.
    assert H.bucket_history([_sample(now - 30 * 3600)], now)["points"] == []


def test_sustained_needs_most_of_the_window():
    now = 50_000.0
    assert H.sustained([_sample(now - 60, cpu=99)], "cpu", now) is None
    full = [_sample(now - 60 * i, cpu=90 + (i % 2)) for i in range(1, 15)]
    assert H.sustained(full, "cpu", now) == 90.5
    assert H.sustained([{"t": now - 60 * i, "cpu": None} for i in range(1, 15)], "cpu", now) is None


class _Store:
    def __init__(self, samples=(), fail=False):
        self.samples, self.fail, self.claimed = list(samples), fail, set()

    async def claim(self, minute):
        if self.fail:
            raise ConnectionError("redis away")
        if minute in self.claimed:
            return False
        self.claimed.add(minute)
        return True

    async def add(self, sample):
        self.samples.append(sample)

    async def read(self, since):
        if self.fail:
            raise ConnectionError("redis away")
        return [s for s in self.samples if s["t"] >= since]


async def _ok_pg():
    return {"ok": True, "size_bytes": 123, "uptime_s": 50.0}


async def _ok_ch():
    return {"ok": True, "uptime_s": 60.0, "size_bytes": 456, "diagnostic_bytes": 7, "memory_bytes": 99}


async def _down():
    raise ConnectionRefusedError("connect to 10.0.0.1:6379 refused")


def test_status_document_reports_down_stores_without_leaking_errors(monkeypatch):
    monkeypatch.setattr(H, "read_software", lambda: {"version": "1.2.3", "installed_at": None, "last_update": None})
    monkeypatch.setattr(H, "read_clock", lambda: {"now": "x", "timezone": "UTC", "ntp_synchronized": True})
    monkeypatch.setattr(H, "_measure_cpu_now", lambda window=0.5: {"usage_pct": 12.0, "iowait_pct": 1.0, "steal_pct": 0.0})
    H._latest.update(at=0.0, cpu=None)
    doc = asyncio.run(H.collect_status(
        probes={"postgresql": _ok_pg, "clickhouse": _ok_ch, "redis": _down},
        store=_Store(fail=True), units=H.parse_systemctl_show(SHOW)))
    by_id = {s["id"]: s for s in doc["services"]}
    assert by_id["cache"]["state"] == "down" and by_id["config_db"]["state"] == "running"
    assert by_id["metrics_db"]["uptime_s"] == 60 and by_id["metrics_db"]["memory_bytes"] == 99
    assert "syslog_receiver" not in by_id          # optional and not installed: no row
    assert by_id["flow_collector"]["state"] == "stopped"
    assert doc["state"] == "critical"
    assert "10.0.0.1" not in str(doc)     # driver errors stay in the log
    stores = {d["id"]: d["size_bytes"] for d in doc["data_stores"]}
    assert stores == {"config_db": 123, "metrics_db": 456, "metrics_db_logs": 7, "cache": None}
    assert doc["cpu"]["usage_pct"] == 12.0 and doc["cpu"]["window_s"] == 1
    assert doc["cpu"]["average_15m_pct"] is None
    assert doc["software"]["version"] == "1.2.3"


def test_the_response_names_services_by_what_they_do_never_by_their_software(monkeypatch):
    """Which database engines and web server the appliance runs on is not part
    of what the page (or its API) says: ids, labels and messages are generic."""
    import json
    import re

    monkeypatch.setattr(H, "read_software", lambda: {"version": "1.2.3", "installed_at": None, "last_update": None})
    monkeypatch.setattr(H, "read_clock", lambda: {"now": "x", "timezone": "UTC", "ntp_synchronized": True})
    monkeypatch.setattr(H, "_measure_cpu_now", lambda window=0.5: None)
    monkeypatch.setattr(H, "cpu_model", lambda: "Fixture CPU")
    H._latest.update(at=0.0, cpu=None)
    units = H.parse_systemctl_show(SHOW.replace("ActiveState=active", "ActiveState=failed"))
    doc = asyncio.run(H.collect_status(
        probes={"postgresql": _down, "clickhouse": _down, "redis": _down}, store=_Store(fail=True), units=units))
    assert doc["state"] == "critical" and len(doc["findings"]) >= 4
    text = json.dumps(doc)
    assert not re.search(r"postgres|clickhouse|redis|nginx|uvicorn|ubuntu|\.service|zenplus-", text, re.I), text
    assert {s["id"] for s in doc["services"]} >= {"api", "monitoring_engine", "config_db", "metrics_db", "cache", "web_server"}
    assert all(set(s) == {"id", "label", "core", "state", "detail", "uptime_s", "memory_bytes", "cpu_seconds", "restarts"}
               for s in doc["services"])
    assert "os" not in doc["host"]


def test_history_says_unavailable_when_the_store_is_away():
    h = asyncio.run(H.history(24, store=_Store(fail=True)))
    assert h["available"] is False and h["points"] == []


def test_history_window_is_clamped_and_short_windows_keep_minutes():
    import time
    now = time.time()
    store = _Store([_sample(int(now) - 120), _sample(int(now) - 60)])
    short = asyncio.run(H.history(1, store=store))
    assert short["bucket_s"] == 60 and short["samples"] == 2
    assert asyncio.run(H.history(9999, store=store))["hours"] == 24
    assert asyncio.run(H.history(24, store=store))["bucket_s"] == 300


def test_one_sample_a_minute_whatever_the_worker_count(monkeypatch):
    """Two workers share one store: each measures, one writes."""
    store = _Store()
    ticks = iter(range(1, 10_000))

    def advancing():
        n = next(ticks)
        return H.CpuTimes(total=1000 * n, idle=600 * n, iowait=0, steal=0)

    monkeypatch.setattr(H, "read_cpu_times", advancing)
    monkeypatch.setattr(H, "read_memory", lambda: H.parse_meminfo(MEMINFO))
    monkeypatch.setattr(H, "read_load", lambda: [1.0, 1.0, 1.0])
    monkeypatch.setattr(H, "read_disk", lambda: {"used_pct": 61.0, "used_bytes": 10, "free_bytes": 5})
    monkeypatch.setattr(H.time, "time", lambda: 6000.0)

    async def run():
        tasks = [asyncio.create_task(H.sampler_loop(store, interval=0.01)) for _ in range(2)]
        await asyncio.sleep(0.03)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())
    assert len(store.samples) == 1
    assert store.samples[0]["mem"] == 75.0 and store.samples[0]["disk_used"] == 10
    assert store.samples[0]["cpu"] == 40.0 and H._latest["cpu"]["usage_pct"] == 40.0


def test_the_sampler_keeps_measuring_while_the_store_is_away(monkeypatch):
    store = _Store(fail=True)
    monkeypatch.setattr(H, "read_cpu_times", lambda: H.parse_cpu_times(STAT_A))
    H._latest.update(at=0.0, cpu="stale")

    async def run():
        task = asyncio.create_task(H.sampler_loop(store, interval=0.01))
        await asyncio.sleep(0.03)
        assert not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert H._latest["cpu"] is None and H._latest["at"] > 0   # measured (no change = unavailable), not stale


def test_status_routes_are_administrator_only():
    from app.core.route_policy import route_inventory
    from app.main import create_app

    rows = [r for r in route_inventory(create_app()) if r["path"].startswith("/api/v1/system/status")]
    assert {r["path"] for r in rows} == {"/api/v1/system/status", "/api/v1/system/status/history"}
    assert all(r["kind"].startswith("guarded") and "system.admin" in r["permissions"] for r in rows), rows
    assert all(r["methods"] == ["GET"] for r in rows)


@pytest.mark.parametrize("pattern,unit,expected", [
    ("postgresql@*.service", "postgresql@16-main.service", True),
    ("postgresql@*.service", "postgresql.service", False),
    ("nginx.service", "nginx.service", True),
    ("nginx.service", "nginx-debug.service", False),
])
def test_unit_patterns(pattern, unit, expected):
    assert H._unit_matches(pattern, unit) is expected
