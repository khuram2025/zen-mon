"""Verify prerequisite artifacts and rollout reachability before promotion."""
import hashlib
import json
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.serialization import load_pem_public_key
from ota.upgrade_path import plan_upgrade


def validate_rollout_path(release, data):
    from django.conf import settings
    from ota.models import Release

    catalog = list(Release.objects.filter(product="ota", arch=release.arch, is_published=True))
    path = plan_upgrade("0.0.0", release, catalog)
    key_path = Path(getattr(settings, "ZENPLUS_RELEASE_PUBLIC_KEY", Path(settings.BASE_DIR) / "keys" / "zenplus-release.pub"))
    key = load_pem_public_key(key_path.read_bytes())
    for step in path:
        package = Path(settings.PACKAGE_STORAGE_PATH) / f"update-{step.version}.zup"
        with package.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != step.package_sha256:
            raise ValueError(f"Package hash mismatch for prerequisite {step.version}")
        with tarfile.open(package, "r:gz") as archive:
            raw = archive.extractfile("manifest.json").read()
            key.verify(archive.extractfile("manifest.json.sig").read(), raw)
            manifest = json.loads(raw)
        for field in ("version", "min_version", "arch"):
            if (manifest.get(field) or None) != (getattr(step, field) or None):
                raise ValueError(f"Signed {field} differs from catalog for {step.version}")
        released = datetime.fromisoformat(manifest["release_date"])
        if released.tzinfo is None:
            released = released.replace(tzinfo=timezone.utc)
        if not datetime.now(timezone.utc) - timedelta(days=30) <= released <= datetime.now(timezone.utc) + timedelta(days=1):
            raise ValueError(f"Prerequisite {step.version} is outside the default manifest age window; publish a supported replacement path")
        if step.pk == release.pk:
            continue
        policy = step.rollout_policies.order_by("-created_at").first()
        if not policy or policy.stage not in ("full", "canary", "percentage") or policy.completed_at is not None:
            raise ValueError(f"Required bridge {step.version} has no active rollout")
        if policy.target_pct != 100:
            raise ValueError(f"Required bridge {step.version} must reach 100% before a dependent rollout")
        if policy.target_group and policy.target_group != data.get("target_group"):
            raise ValueError(f"Required bridge {step.version} does not cover the target group")
    return [step.version for step in path]
