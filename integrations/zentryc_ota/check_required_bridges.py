"""Daily read-only validation of the signed route available to returning appliances."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from ota.models import Release
from ota.rollout_guard import validate_rollout_path
from ota.upgrade_path import version_key


class Command(BaseCommand):
    help = "Verify the full rollout path from the oldest supported returning appliance"

    def handle(self, **options):
        baseline = getattr(settings, "ZENPLUS_OTA_OLDEST_SUPPORTED", "1.24.29")
        releases = Release.objects.filter(product="ota", is_published=True).prefetch_related("rollout_policies")
        targets = {}
        for release in releases:
            policy = max(release.rollout_policies.all(), key=lambda p: p.created_at, default=None)
            if (policy and policy.stage == "full" and not policy.target_group
                    and policy.target_pct == 100 and policy.completed_at is None):
                previous = targets.get(release.arch)
                if previous is None or version_key(release.version) > version_key(previous.version):
                    targets[release.arch] = release
        if not targets:
            raise CommandError("No published full rollout covers returning appliances")
        for release in targets.values():
            try:
                steps = validate_rollout_path(release, {}, installed=baseline, valid_for_days=7)
            except (ValueError, OSError) as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(f"{release.arch}: " + " -> ".join([baseline, *steps]))
