"""API side of the watchdog: policy hand-off, audit import, findings, backpressure."""
from __future__ import annotations

import json
import time

import pytest
from fastapi import HTTPException

from app.services import storage_backpressure, storage_layout as L, storage_service as svc
from app.services import storage_watchdog as W


def test_policy_written_for_the_watchdog_is_normalized():
    assert svc.write_watchdog_policy({"auto_purge": {"enabled": True, "threshold_pct": 90,
                                                     "target_pct": 80, "min_keep_days": 14,
                                                     "min_free_gib": 20}})
    stored = json.loads(open(W._path("policy.json")).read())
    assert stored["policy"]["threshold_pct"] == 90 and stored["policy"]["min_free_gib"] == 20
    assert W.Watchdog(client_factory=lambda: None, sampler=lambda: []).policy()["min_keep_days"] == 14


def test_watchdog_status_reports_liveness_and_trims_series():
    assert svc.watchdog_status() == {"installed": False, "alive": False}
    W._atomic_write("state.json", {"heartbeat": time.time(), "samples": {"1": [[0, 1]]},
                                   "state": "ok", "filesystems": []})
    status = svc.watchdog_status()
    assert status["alive"] and status["installed"] and "samples" not in status
    W._atomic_write("state.json", {"heartbeat": time.time() - 3600, "state": "ok"})
    assert svc.watchdog_status()["alive"] is False


class _DB:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.inserted: list[dict] = []

    async def execute(self, statement, params=None):
        sql = str(statement)

        class R:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows
        if sql.startswith("SELECT details->>'watchdog_id'"):
            return R([(i,) for i in params["ids"] if i in self.existing])
        self.inserted.append(params)
        return R([])


@pytest.mark.asyncio
async def test_audit_import_is_idempotent_and_separates_scheduled_from_reclaimed():
    now = time.time()
    a = W.append_audit("watchdog_purge", {"scheduled_bytes": 5 << 30}, now)
    b = W.append_audit("watchdog_reclaim", {"scheduled_bytes": 5 << 30,
                                            "measured_free_change_bytes": 4 << 30}, now)
    db = _DB(existing={a["id"]})
    assert await svc.import_watchdog_audit(db) == 1
    row = db.inserted[0]
    assert row["t"] == "watchdog_reclaim" and row["f"] == 4 << 30
    assert json.loads(row["d"])["watchdog_id"] == b["id"]


def test_watchdog_findings():
    hb = time.time()
    issues = L.watchdog_issues({"installed": True, "alive": True, "heartbeat": hb,
                                "ingest_paused": True,
                                "filesystems": [{"mount": "/ /data", "state": "critical",
                                                 "exhausted": True},
                                                {"mount": "/x", "state": "growth",
                                                 "runway_hours": 4.2,
                                                 "growth_bytes_per_hour": 8 << 30}],
                                "last_error": {"at": hb - 60, "message": "ClickHouse: down"}})
    codes = [i["code"] for i in issues]
    assert codes[:2] == ["ingest_paused", "purge_exhausted"]
    assert {"fast_growth", "watchdog_error"} <= set(codes)
    assert "4.2 h" in next(i["title"] for i in issues if i["code"] == "fast_growth")
    assert [i["code"] for i in L.watchdog_issues({"installed": True, "alive": False,
                                                  "heartbeat_age_s": 600})] == ["watchdog_stale"]


def test_backpressure_answers_503_with_retry_after_while_paused():
    storage_backpressure.raise_if_paused("Host telemetry")      # not paused: no error
    with open(W._path("ingest-paused"), "w") as f:
        json.dump({"since": time.time(), "reason": "/: free 1.0 GiB < 5.0 GiB"}, f)
    storage_backpressure._cache.update({"at": 0.0, "value": None})
    with pytest.raises(HTTPException) as exc:
        storage_backpressure.raise_if_paused("Host telemetry")
    assert exc.value.status_code == 503 and exc.value.headers["Retry-After"] == "300"
    assert "free 1.0 GiB" in exc.value.detail


def test_paused_ingest_rejects_host_results_before_claiming_the_batch(monkeypatch):
    from app.api.v1 import agents

    with open(W._path("ingest-paused"), "w") as f:
        json.dump({"reason": "critical"}, f)
    storage_backpressure._cache.update({"at": 0.0, "value": None})
    src = open(agents.__file__).read()
    pause_at = src.index('storage_backpressure.raise_if_paused("Host telemetry")')
    assert pause_at < src.index("payload_sha256 = _host_results_batch_digest(data)")
