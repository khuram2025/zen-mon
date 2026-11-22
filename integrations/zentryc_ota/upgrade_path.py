"""ZenPlus-only OTA dependency planning. Deploy as ota/upgrade_path.py on Zentryc."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ACTIVE_STAGES = ("canary", "percentage", "full")
FAILED_STATES = ("failed", "rolled_back")
# A rollout policy's max_failure_pct is the share of appliances that may fail a
# release before it stops being offered. 100 turns the automatic stop off.
DEFAULT_MAX_FAILURE_PCT = 5
# The automatic stop judges rollouts created from this moment on. Older
# rollouts (the 1.24.36 bridge among them) carry failures from appliances that
# have long since moved on or been retired; pausing them retroactively would
# block every appliance behind a required checkpoint.
AUTO_STOP_FROM = datetime(2026, 11, 17, tzinfo=timezone.utc)


class UpgradePathError(ValueError):
    pass


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise UpgradePathError(f"Invalid version: {value!r}")
    return tuple(map(int, value.split(".")))


def plan_upgrade(current, target, catalog):
    """Return required releases in install order, or fail without a partial path.

    catalog contains published, product/architecture-scoped releases. The caller
    must check rollout eligibility for EVERY returned step before offering any.
    min_version names a required bridge when the installed version is below it.
    Cumulative releases need no artificial hop for each intervening version.
    """
    installed = version_key(current)
    by_version = {r.version: r for r in catalog}
    path = []
    visiting = set()

    def visit(release):
        candidate = version_key(release.version)
        if candidate <= installed:
            return
        if release.version in visiting:
            raise UpgradePathError("Cyclic upgrade path")
        visiting.add(release.version)
        minimum = release.min_version
        if minimum:
            minimum_key = version_key(minimum)
            if minimum_key >= candidate:
                raise UpgradePathError(f"Invalid prerequisite for {release.version}: {minimum}")
            if installed < minimum_key:
                bridge = by_version.get(minimum)
                if bridge is None:
                    raise UpgradePathError(f"Required bridge {minimum} is missing or unpublished")
                visit(bridge)
        path.append(release)
        visiting.remove(release.version)

    visit(target)
    return path


def failure_halt(outcomes, max_failure_pct=None):
    """Whether a release has failed on too many appliances to keep offering it.

    outcomes are (appliance, status) pairs for one release. An appliance counts
    as failed while it has a failed or rolled-back attempt and no success, so a
    retry that succeeds clears it. Returns (halt, failed, finished): finished is
    the number of appliances with a final result. Unfinished attempts are left
    out. A single unresolved failure halts until enough appliances succeed to
    bring the failed share back to max_failure_pct or below.
    """
    limit = DEFAULT_MAX_FAILURE_PCT if max_failure_pct is None else max_failure_pct
    succeeded = {appliance for appliance, status in outcomes if status == "success"}
    failed = {appliance for appliance, status in outcomes if status in FAILED_STATES} - succeeded
    finished = len(succeeded) + len(failed)
    halt = bool(failed) and len(failed) * 100 > limit * finished
    return halt, len(failed), finished


def auto_stop_applies(policy):
    created = getattr(policy, "created_at", None)
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created >= AUTO_STOP_FROM


def halt_failing_rollouts(rollouts):
    """Pause each active rollout whose release keeps failing in the field.

    rollouts maps version to the release's latest policy (or None). A paused
    policy stays paused: offering the release again takes a new, reviewed
    rollout (with a higher max_failure_pct if the failures are understood and
    accepted). Returns the versions paused by this call.
    """
    from ota.models import RolloutPolicy, UpdateHistory

    active = {policy.release_id: (version, policy) for version, policy in rollouts.items()
              if policy and policy.stage in ACTIVE_STAGES and policy.completed_at is None
              and auto_stop_applies(policy)}
    if not active:
        return []
    outcomes = {release_id: [] for release_id in active}
    rows = UpdateHistory.objects.filter(
        release_id__in=list(active), status__in=("success",) + FAILED_STATES,
    ).values_list("release_id", "appliance_id", "status")
    for release_id, appliance_id, status in rows:
        outcomes[release_id].append((appliance_id, status))
    paused = []
    for release_id, (version, policy) in active.items():
        halt, failed, finished = failure_halt(outcomes[release_id], getattr(policy, "max_failure_pct", None))
        if not halt:
            continue
        RolloutPolicy.objects.filter(pk=policy.pk, stage=policy.stage).update(stage="paused")
        policy.stage = "paused"
        paused.append(version)
        logger.error("ZenPlus rollout of %s paused automatically: %d of %d appliances failed to install it",
                     version, failed, finished)
    return paused


def rollout_allows(appliance, release, rollout):
    if not rollout or rollout.stage not in ACTIVE_STAGES:
        return False
    if rollout.completed_at is not None:
        return False
    if rollout.target_group and rollout.target_group != appliance.rollout_group:
        return False
    if not 0 < rollout.target_pct <= 100:
        return False
    bucket = int(hashlib.sha256(f"{appliance.id}:{release.id}".encode()).hexdigest(), 16) % 100
    return bucket < rollout.target_pct


def get_zenplus_update(appliance):
    from ota.models import Release, UpdateHistory

    if not appliance.is_active:
        return None
    catalog = list(Release.objects.filter(
        product="ota", arch=appliance.arch, is_published=True,
    ).prefetch_related("rollout_policies"))
    rollouts = {r.version: next(iter(sorted(r.rollout_policies.all(),
                key=lambda p: p.created_at, reverse=True)), None) for r in catalog}
    # A release that fails in the field stops for everyone, not only for the
    # appliance that failed three times.
    halt_failing_rollouts(rollouts)
    eligible = [r for r in catalog if rollout_allows(appliance, r, rollouts[r.version])]
    if not eligible:
        return None
    try:
        target = max(eligible, key=lambda r: version_key(r.version))
        path = plan_upgrade(appliance.current_version, target, catalog)
    except UpgradePathError as exc:
        appliance.upgrade_hold = str(exc)
        logger.warning("ZenPlus upgrade path blocked for appliance %s: %s", appliance.id, exc)
        return None
    for release in path:
        if not rollout_allows(appliance, release, rollouts[release.version]):
            appliance.upgrade_hold = f"Required release v{release.version} is outside this appliance’s active rollout."
            logger.warning("ZenPlus upgrade blocked: prerequisite %s is not eligible", release.version)
            return None
        history = UpdateHistory.objects.filter(appliance=appliance, release=release)
        if history.filter(status="success").exists():
            appliance.upgrade_hold = f"Update history records v{release.version} as installed. Check the appliance version."
            return None  # inconsistent installed marker requires investigation
        if history.filter(status__in=["failed", "rolled_back"]).count() >= 3:
            appliance.upgrade_hold = f"Installation of v{release.version} is on hold after three failed attempts."
            return None  # never route around a failed prerequisite
    return path[0] if path else None
