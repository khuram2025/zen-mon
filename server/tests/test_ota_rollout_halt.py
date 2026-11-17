"""Automatic rollout stop and required checkpoints.

The portal pauses a release that keeps failing in the field (integrations/zentryc_ota/upgrade_path.py), and
scripts/release_policy.py makes the newest required checkpoint below a release its signed minimum. The portal
module is run here without Django: only the two models it queries are stood in for.
"""
from __future__ import annotations

import json
import shutil
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from integrations.zentryc_ota import upgrade_path  # noqa: E402
from integrations.zentryc_ota.upgrade_path import failure_halt  # noqa: E402


# ── failure_halt ────────────────────────────────────────────────────────────

def test_no_results_or_only_successes_never_halt():
    assert failure_halt([]) == (False, 0, 0)
    assert failure_halt([("a", "success"), ("b", "success")]) == (False, 0, 2)


def test_one_unresolved_failure_halts_a_small_fleet():
    # The 1.25.10 case: .221 succeeded, one production appliance failed.
    assert failure_halt([("dev", "success"), ("prod", "failed")]) == (True, 1, 2)
    assert failure_halt([("prod", "rolled_back")]) == (True, 1, 1)


def test_repeated_attempts_count_one_appliance_and_a_success_clears_it():
    assert failure_halt([("prod", "failed"), ("prod", "failed"), ("prod", "rolled_back")]) == (True, 1, 1)
    assert failure_halt([("prod", "failed"), ("prod", "success")]) == (False, 0, 1)


def test_unfinished_attempts_are_not_results():
    assert failure_halt([("a", "downloading"), ("b", "applying")]) == (False, 0, 0)
    assert failure_halt([("a", "downloading"), ("b", "failed")]) == (True, 1, 1)


def test_limit_is_a_share_of_finished_appliances():
    nineteen = [(f"ok{i}", "success") for i in range(19)]
    assert failure_halt(nineteen + [("bad", "failed")])[0] is False          # 1 of 20 = 5 %: at the limit
    assert failure_halt(nineteen[:18] + [("bad", "failed")])[0] is True      # 1 of 19 > 5 %
    assert failure_halt([("ok", "success"), ("bad", "failed")], 50)[0] is False
    assert failure_halt([("bad", "failed")], 100)[0] is False                # 100 turns the stop off
    assert failure_halt([("ok", "success")] * 1 + [("bad", "failed")], 0)[0] is True


# ── halt_failing_rollouts ───────────────────────────────────────────────────

@pytest.fixture
def portal(monkeypatch):
    """Stand in for ota.models: update history rows and rollout policies that record a pause."""
    history: list[tuple] = []
    updates: list[dict] = []

    class HistoryQuery:
        def filter(self, *, release_id__in, status__in):
            self.rows = [r for r in history if r[0] in release_id__in and r[2] in status__in]
            return self

        def values_list(self, *fields):
            assert fields == ("release_id", "appliance_id", "status")
            return self.rows

    class PolicyQuery:
        def filter(self, **where):
            self.where = where
            return self

        def update(self, **values):
            updates.append({"where": self.where, "set": values})

    models = types.ModuleType("ota.models")
    models.UpdateHistory = NS(objects=HistoryQuery())
    models.RolloutPolicy = NS(objects=PolicyQuery())
    monkeypatch.setitem(sys.modules, "ota", types.ModuleType("ota"))
    monkeypatch.setitem(sys.modules, "ota.models", models)
    return history, updates


NEW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)


def policy(pk, release_id, stage="full", *, max_failure_pct=5, completed_at=None, created_at=NEW):
    return NS(pk=pk, release_id=release_id, stage=stage, max_failure_pct=max_failure_pct, completed_at=completed_at,
              created_at=created_at, target_group=None, target_pct=100)


def test_failing_release_is_paused_and_healthy_ones_are_left(portal):
    history, updates = portal
    history += [("r10", "dev", "success"), ("r10", "prod", "failed"), ("r9", "dev", "success"),
                ("r9", "other", "downloading")]
    rollouts = {"1.25.10": policy(10, "r10"), "1.25.9": policy(9, "r9"), "1.25.8": None}
    assert upgrade_path.halt_failing_rollouts(rollouts) == ["1.25.10"]
    assert updates == [{"where": {"pk": 10, "stage": "full"}, "set": {"stage": "paused"}}], \
        "only the stage it read is replaced, and only for the failing release"
    assert rollouts["1.25.10"].stage == "paused" and rollouts["1.25.9"].stage == "full"
    # The selector treats it like any paused rollout from now on.
    appliance = NS(id="a1", rollout_group="stable")
    assert upgrade_path.rollout_allows(appliance, NS(id="r10"), rollouts["1.25.10"]) is False
    assert upgrade_path.rollout_allows(appliance, NS(id="r9"), rollouts["1.25.9"]) is True


def test_paused_completed_and_overridden_rollouts_are_not_touched(portal):
    history, updates = portal
    history += [("r1", "x", "failed"), ("r2", "x", "failed"), ("r3", "x", "failed")]
    rollouts = {"1.0.1": policy(1, "r1", "paused"), "1.0.2": policy(2, "r2", completed_at="2026-11-01"),
                "1.0.3": policy(3, "r3", max_failure_pct=100)}
    assert upgrade_path.halt_failing_rollouts(rollouts) == []
    assert updates == []


def test_canary_rollout_pauses_too_and_a_missing_limit_uses_the_default(portal):
    history, updates = portal
    history.append(("r5", "canary-box", "rolled_back"))
    rollouts = {"1.0.5": NS(pk=5, release_id="r5", stage="canary", completed_at=None, created_at=NEW)}
    assert upgrade_path.halt_failing_rollouts(rollouts) == ["1.0.5"]
    assert updates == [{"where": {"pk": 5, "stage": "canary"}, "set": {"stage": "paused"}}]


def test_rollouts_from_before_the_automatic_stop_are_never_paused_retroactively(portal):
    # The 1.24.36 bridge has old failures from appliances that moved on; pausing it would block the fleet.
    history, updates = portal
    history += [("bridge", "old-box", "failed"), ("fresh", "prod", "failed")]
    before = upgrade_path.AUTO_STOP_FROM - timedelta(days=20)
    rollouts = {"1.24.36": policy(1, "bridge", created_at=before),
                "1.24.35": policy(2, "bridge", created_at=before.replace(tzinfo=None)),   # naive timestamps are UTC
                "1.24.34": NS(pk=3, release_id="bridge", stage="full", completed_at=None),     # no timestamp: left alone
                "1.25.14": policy(4, "fresh", created_at=upgrade_path.AUTO_STOP_FROM)}
    assert upgrade_path.halt_failing_rollouts(rollouts) == ["1.25.14"]
    assert [u["where"]["pk"] for u in updates] == [4]


# ── required checkpoints ────────────────────────────────────────────────────

def policy_root(tmp_path, **changes):
    (tmp_path / "scripts").mkdir()
    document = json.loads((ROOT / "scripts" / "release-policy.json").read_text())
    document.update(changes)
    (tmp_path / "scripts" / "release-policy.json").write_text(json.dumps(document))
    for name in document["required_payload_files"]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    return tmp_path


def test_shipped_policy_lists_its_checkpoints_with_reasons():
    from release_policy import default_minimum, load_policy
    shipped = load_policy(ROOT)
    assert shipped["required_checkpoints"][-1]["version"] == shipped["minimum_supported_upgrade_base"]
    assert all(entry["reason"].strip() for entry in shipped["required_checkpoints"])
    assert default_minimum(ROOT, "1.25.14") == "1.24.36"


def test_a_release_requires_the_newest_checkpoint_below_it(tmp_path):
    from release_policy import default_minimum, validate_policy
    root = policy_root(tmp_path, minimum_supported_upgrade_base="1.26.0", required_checkpoints=[
        {"version": "1.24.36", "reason": "installer catalog"}, {"version": "1.26.0", "reason": "one-time data transform"}])
    # The checkpoint release itself is reached from the checkpoint before it.
    assert default_minimum(root, "1.26.0") == "1.24.36"
    assert validate_policy(root, "1.26.0", None) == "1.24.36"
    # Everything after it must pass through it.
    assert validate_policy(root, "1.26.1", None) == "1.26.0"
    assert validate_policy(root, "1.27.0", "1.26.1") == "1.26.1"
    with pytest.raises(ValueError, match="bridge floor 1.26.0"):
        validate_policy(root, "1.26.3", "1.25.9")
    # A release built before the new checkpoint keeps the older one.
    assert validate_policy(root, "1.25.20", None) == "1.24.36"
    with pytest.raises(ValueError, match="not above any required checkpoint"):
        default_minimum(root, "1.24.36")


@pytest.mark.parametrize("changes,message", [
    (dict(required_checkpoints=[{"version": "1.26.0", "reason": "x"}, {"version": "1.24.36", "reason": "y"}]),
     "ascending"),
    (dict(required_checkpoints=[{"version": "1.24.36", "reason": "y"}, {"version": "1.24.36", "reason": "y"}]),
     "unique"),
    (dict(required_checkpoints=[{"version": "1.24.36", "reason": " "}]), "needs a reason"),
    (dict(minimum_supported_upgrade_base="1.26.0"), "must be the newest required checkpoint"),
    (dict(minimum_supported_upgrade_base="1.24.37",
          required_checkpoints=[{"version": "1.24.36", "reason": "y"}, {"version": "1.24.37", "reason": "z"}]),
     "blocked prerequisite"),
])
def test_inconsistent_checkpoint_lists_are_refused(tmp_path, changes, message):
    from release_policy import load_policy
    with pytest.raises(ValueError, match=message):
        load_policy(policy_root(tmp_path, **changes))


def test_policy_without_a_checkpoint_list_keeps_the_floor(tmp_path):
    from release_policy import validate_policy
    root = policy_root(tmp_path)
    document = json.loads((root / "scripts" / "release-policy.json").read_text())
    del document["required_checkpoints"]
    (root / "scripts" / "release-policy.json").write_text(json.dumps(document))
    assert validate_policy(root, "1.25.14", None) == "1.24.36"
