"""Services list: cached finished days for the 30-day bars, and when a
failing monitor entered its state."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core import database
from app.services import service_availability, service_check_service as svc, service_maintenance


class Res:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return list(self.rows)


class DB:
    def __init__(self, ids=("a",)):
        self.ids = ids

    async def execute(self, stmt, params=None):
        return Res([(cid, None, 60, None, ["team-a"]) for cid in self.ids])


def _setup(monkeypatch):
    svc.clear_fleet_cache()
    scans = []

    def read(ch, checks, start, end):
        scans.append((start, end))
        for cid in checks:
            yield cid, [(start, end, 1)], 1, start

    async def maint(db, rows, start, end):
        return {}
    monkeypatch.setattr(database, "get_clickhouse_client", lambda: None)
    monkeypatch.setattr(service_availability, "read_fleet_segments", read)
    monkeypatch.setattr(service_maintenance, "intervals_for_checks", maint)
    return scans


def _bars(db, tz="Asia/Riyadh"):
    svc._fleet_cache.clear()  # the per-minute response cache is not under test
    return asyncio.run(svc.get_confirmed_uptime_all(db, days=30, tz_name=tz, visible_tags=["team-a"]))


def test_finished_days_are_scanned_once_and_today_every_time(monkeypatch):
    scans = _setup(monkeypatch)
    first = _bars(DB())
    assert len(scans) == 2                      # today + finished days
    today_start = max(lo for lo, _hi in scans)
    second = _bars(DB())
    assert len(scans) == 3 and scans[-1][0] == today_start   # only today again
    rows = first["checks"]["a"]
    assert len(rows) == 30 and [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    # summarize() divides float seconds: 100 % can come back as 99.99999999999999.
    assert [r["uptime_pct"] for r in rows] == pytest.approx([100.0] * 30)
    assert [r["date"] for r in second["checks"]["a"]] == [r["date"] for r in rows]
    svc.clear_fleet_cache()


def test_a_new_monitor_rescans_finished_days(monkeypatch):
    scans = _setup(monkeypatch)
    _bars(DB(("a",)))
    out = _bars(DB(("a", "b")))
    assert len(scans) == 4                      # the check set changed: no stale cache
    assert len(out["checks"]["b"]) == 30
    svc.clear_fleet_cache()


def test_expired_finished_days_are_served_then_refreshed_in_background(monkeypatch):
    scans = _setup(monkeypatch)
    _bars(DB())
    (key, (stamp, value)), = svc._closed_days_cache.items()
    svc._closed_days_cache[key] = (stamp - svc.CLOSED_DAYS_FRESH_SEC - 1, value)
    out = _bars(DB())                           # asyncio.run waits for the executor
    assert len(out["checks"]["a"]) == 30
    assert len(scans) == 4                      # today + background refresh
    assert svc._closed_days_cache[key][0] > stamp and not svc._closed_days_refreshing
    svc.clear_fleet_cache()


def test_status_since_matches_only_the_current_state():
    now = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    checks = [SimpleNamespace(id="d", status="down"), SimpleNamespace(id="w", status="degraded"),
              SimpleNamespace(id="x", status="down")]
    rows = [("d", "down", (now - timedelta(hours=2)).replace(tzinfo=None)),
            ("w", "warning", now - timedelta(minutes=5)),
            ("x", "up", now - timedelta(minutes=1))]   # log disagrees: unknown, not guessed
    out = svc.status_since_from_rows(checks, rows)
    assert out == {"d": now - timedelta(hours=2), "w": now - timedelta(minutes=5)}


def test_status_since_skips_healthy_and_paused_and_survives_errors(monkeypatch):
    now = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("clickhouse unavailable")
    monkeypatch.setattr(database, "get_clickhouse_client", boom)
    healthy = [SimpleNamespace(id="u", status="up", enabled=True),
               SimpleNamespace(id="p", status="down", enabled=False)]
    assert asyncio.run(svc.status_since_for(healthy, now)) == {} and not calls
    failing = [SimpleNamespace(id="d", status="down", enabled=True)]
    assert asyncio.run(svc.status_since_for(failing, now)) == {} and calls
