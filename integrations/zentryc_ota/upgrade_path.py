"""ZenPlus-only OTA dependency planning. Deploy as ota/upgrade_path.py on Zentryc."""
from __future__ import annotations

import hashlib
import logging
import re

logger = logging.getLogger(__name__)


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


def rollout_allows(appliance, release, rollout):
    if not rollout or rollout.stage not in ("canary", "percentage", "full"):
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
    eligible = [r for r in catalog if rollout_allows(appliance, r, rollouts[r.version])]
    if not eligible:
        return None
    try:
        target = max(eligible, key=lambda r: version_key(r.version))
        path = plan_upgrade(appliance.current_version, target, catalog)
    except UpgradePathError as exc:
        logger.warning("ZenPlus upgrade path blocked for appliance %s: %s", appliance.id, exc)
        return None
    for release in path:
        if not rollout_allows(appliance, release, rollouts[release.version]):
            logger.warning("ZenPlus upgrade blocked: prerequisite %s is not eligible", release.version)
            return None
        history = UpdateHistory.objects.filter(appliance=appliance, release=release)
        if history.filter(status="success").exists():
            return None  # inconsistent installed marker requires investigation
        if history.filter(status__in=["failed", "rolled_back"]).count() >= 3:
            return None  # never route around a failed prerequisite
    return path[0] if path else None
