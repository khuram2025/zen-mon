"""Tests for the collector framework.

The collectors themselves shell out to subprocess / asyncpg / docker, so
exhaustive happy-path tests would require a real appliance. Instead we
target the invariants that must hold regardless of what any collector
actually does:

- One crashing collector must not abort the bundle.
- ``CollectorResult.summary()`` serializes cleanly into the manifest.
- ``all_collectors()`` returns every Phase-1+2 collector in the documented
  order — so renaming a collector silently is caught here.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from support.collectors import (  # noqa: E402
    CollectorContext,
    CollectorResult,
    all_collectors,
    run_collector,
)


def _ctx(time_range: str = "24h") -> CollectorContext:
    return CollectorContext(
        job_id="00000000-0000-0000-0000-000000000000",
        time_range=time_range,
        include_extended_logs=False,
        zenplus_root=REPO_ROOT,  # use repo as the "appliance" root
        updater_root=REPO_ROOT / "updater",
    )


def test_run_collector_isolates_exceptions():
    def boom(_ctx):
        raise RuntimeError("simulated crash")

    result = run_collector("boom", boom, _ctx())
    assert result.status == "failed"
    assert any("simulated crash" in n for n in result.notes)
    assert result.completed_at is not None


def test_run_collector_records_ok_when_collector_passes():
    def happy(_ctx):
        r = CollectorResult(section="happy")
        r.files["happy/info.txt"] = b"hi"
        return r

    result = run_collector("happy", happy, _ctx())
    assert result.status == "ok"
    assert "happy/info.txt" in result.files


def test_result_summary_serializes_cleanly():
    import json
    r = CollectorResult(section="x")
    r.files["a/b.txt"] = b"hello"
    r.warn("a warning")
    r.completed_at = r.started_at  # avoid None for the JSON
    body = json.dumps(r.summary())
    assert "a/b.txt" in body
    assert '"status": "warning"' in body


def test_all_collectors_runs_phase1_and_phase2_in_order():
    names = [name for name, _ in all_collectors()]
    expected = [
        "inventory",
        "health",
        "logs",
        "database",
        "clickhouse",
        "config_files",
        "network",
        "storage",
        "updates",
        "features",
    ]
    assert names == expected


def test_collector_context_translates_time_range():
    assert _ctx("1h").since_arg() == "1 hour ago"
    assert _ctx("7d").since_arg() == "7 days ago"
    # Unknown range falls back to the safe 24h default.
    assert _ctx("nonsense").since_arg() == "24 hours ago"


def test_one_failing_collector_does_not_abort_others():
    """Simulate the worker loop: register one crashing + one happy collector
    and assert both produce summaries the manifest can serialize."""
    def boom(_ctx):
        raise ValueError("nope")

    def happy(_ctx):
        r = CollectorResult(section="happy")
        r.files["happy.txt"] = b"hi"
        return r

    summaries = {}
    for name, fn in [("boom", boom), ("happy", happy)]:
        result = run_collector(name, fn, _ctx())
        summaries[name] = result.summary()

    assert summaries["boom"]["status"] == "failed"
    assert summaries["happy"]["status"] == "ok"
    # The bundle continues — happy still got its file in.
    assert summaries["happy"]["files"] == ["happy.txt"]
