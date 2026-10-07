"""Scheduler tick: a due schedule is claimed (next_run_at advanced and
committed) before generation, and timer-fired custom schedules keep their
custom_report_id."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app.services import report_scheduler as rs


class TickDB:
    def __init__(self, rows):
        self.rows = rows
        self.log: list[str] = []

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.log.append(sql[:40])
        rows = self.rows

        class R:
            def fetchall(self):
                return rows if sql.startswith("SELECT * FROM report_schedules") else []

        return R()

    async def commit(self):
        self.log.append("COMMIT")

    async def rollback(self):
        self.log.append("ROLLBACK")


def _row(**kw):
    base = dict(id="s1", name="Monthly SLA", enabled=True, report_type="custom", period="last_30d",
                format="pdf", filters={}, frequency="daily", hour=8, minute=0, day_of_week=None,
                day_of_month=None, notify_channels=[], custom_report_id="c-123")
    base.update(kw)
    return SimpleNamespace(**base)


def test_tick_claims_before_generating_and_keeps_custom_id(monkeypatch):
    seen = {}

    async def fake_tz(db):
        return "Asia/Riyadh"

    async def fake_generate(db, sched, **kw):
        seen["sched"] = sched
        seen["log_at_generate"] = list(db.log)
        return {"status": "success", "errors": []}

    monkeypatch.setattr(rs, "get_configured_timezone", fake_tz)
    monkeypatch.setattr(rs, "generate_and_deliver", fake_generate)
    db = TickDB([_row()])
    out = asyncio.run(rs.run_scheduler_tick(db))

    assert out["fired"] == ["s1"]
    assert seen["sched"]["custom_report_id"] == "c-123"
    # The claim (UPDATE next_run_at) was committed before generation started.
    before = seen["log_at_generate"]
    assert before[-1] == "COMMIT" and any(s.startswith("UPDATE report_schedules") for s in before)


def test_tick_failure_records_class_name_not_message(monkeypatch):
    async def fake_tz(db):
        return "UTC"

    async def boom(db, sched, **kw):
        raise RuntimeError("password=secret in DSN")

    captured = []
    db = TickDB([_row(report_type="availability", custom_report_id=None)])
    orig = db.execute

    async def spy(statement, params=None):
        if params and "st" in params:
            captured.append(params)
        return await orig(statement, params)

    db.execute = spy
    monkeypatch.setattr(rs, "get_configured_timezone", fake_tz)
    monkeypatch.setattr(rs, "generate_and_deliver", boom)
    asyncio.run(rs.run_scheduler_tick(db))
    assert captured and captured[-1]["st"] == "failed" and captured[-1]["err"] == "RuntimeError"
    assert "ROLLBACK" in db.log


def test_row_without_custom_column_is_tolerated():
    row = _row()
    del row.custom_report_id
    assert rs._row_to_sched(row)["custom_report_id"] is None
    assert isinstance(rs.compute_next_run(rs._row_to_sched(_row()), "UTC",
                                          after=datetime(2026, 10, 7, tzinfo=timezone.utc)), datetime)
