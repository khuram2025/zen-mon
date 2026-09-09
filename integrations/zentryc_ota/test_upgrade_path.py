"""Run with Django's test runner as ota.test_upgrade_path on the OTA host."""
from django.test import TestCase
from django.utils import timezone
from ota.models import Appliance, Release, RolloutPolicy, Subscription, UpdateHistory
from ota.services import get_available_update


class RequiredUpgradePathTests(TestCase):
    def setUp(self):
        sub = Subscription.objects.create(name="Upgrade path test", subscription_type="ota", plan="starter", max_appliances=10, is_active=True)
        self.appliance = Appliance.objects.create(subscription=sub, api_key_hash="path-test", hostname="path-test", arch="amd64", current_version="1.23.3", rollout_group="stable", is_active=True)
        self.releases = []
        for number, minimum in [(7, None), (8, "1.23.7"), (9, "1.23.8"), (10, "1.23.9")]:
            r = Release.objects.create(product="ota", version=f"1.23.{number}", min_version=minimum, arch="amd64", package_url="https://example.test/test.zup", package_sha256="0"*64, manifest_sig="test", is_published=True)
            RolloutPolicy.objects.create(release=r, stage="full", target_pct=100)
            self.releases.append(r)

    def test_each_successful_hop_advances_to_next_prerequisite(self):
        for r in self.releases:
            self.assertEqual(get_available_update(self.appliance), r)
            UpdateHistory.objects.create(appliance=self.appliance, release=r, status="success", attempt=1)
            self.appliance.current_version = r.version
        self.assertIsNone(get_available_update(self.appliance))

    def test_missing_bridge_blocks_whole_chain(self):
        self.releases[1].delete()
        self.assertIsNone(get_available_update(self.appliance))

    def test_paused_bridge_does_not_resurrect_older_full_rollout(self):
        RolloutPolicy.objects.create(release=self.releases[1], stage="paused", target_pct=100)
        self.assertIsNone(get_available_update(self.appliance))

    def test_unpublished_bridge_blocks_chain(self):
        r = self.releases[1]
        r.is_published = False
        r.save()
        self.assertIsNone(get_available_update(self.appliance))

    def test_bridge_in_different_group_blocks_chain(self):
        RolloutPolicy.objects.create(release=self.releases[1], stage="canary", target_pct=100, target_group="canary")
        self.assertIsNone(get_available_update(self.appliance))

    def test_three_failed_bridge_attempts_cannot_be_skipped(self):
        for attempt in (1, 2, 3):
            UpdateHistory.objects.create(appliance=self.appliance, release=self.releases[0], status="failed", attempt=attempt)
        self.assertIsNone(get_available_update(self.appliance))

    def test_other_product_cannot_supply_missing_bridge(self):
        r = self.releases[1]
        r.product = "zenai"
        r.save()
        self.assertIsNone(get_available_update(self.appliance))

    def test_other_arch_cannot_supply_missing_bridge(self):
        r = self.releases[1]
        r.arch = "arm64"
        r.save()
        self.assertIsNone(get_available_update(self.appliance))

    def test_new_unrolled_release_does_not_hide_stable_chain(self):
        Release.objects.create(product="ota", version="1.23.11", min_version="1.23.10", arch="amd64", package_url="https://example.test/test.zup", package_sha256="0"*64, manifest_sig="test", is_published=True)
        self.assertEqual(get_available_update(self.appliance), self.releases[0])

    def test_unknown_version_requires_repair(self):
        self.appliance.current_version = "unknown"
        self.assertIsNone(get_available_update(self.appliance))

    def artifact_fixture(self, folder, *, expire=False):
        import hashlib, io, json, tarfile
        from pathlib import Path
        from datetime import timedelta
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        key = Ed25519PrivateKey.generate()
        public = Path(folder) / "release.pub"
        public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        for r in self.releases:
            manifest = json.dumps(dict(version=r.version, min_version=r.min_version, arch=r.arch,
                release_date=(timezone.now() - timedelta(days=31 if expire else 0)).isoformat())).encode()
            package = Path(folder) / f"update-{r.version}.zup"
            with tarfile.open(package, "w:gz") as archive:
                for name, data in [("manifest.json", manifest), ("manifest.json.sig", key.sign(manifest))]:
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            r.package_sha256 = hashlib.sha256(package.read_bytes()).hexdigest()
            r.save()
        return public

    def test_promotion_verifies_real_signed_chain(self):
        import tempfile
        from django.test import override_settings
        from ota.rollout_guard import validate_rollout_path
        with tempfile.TemporaryDirectory() as folder:
            public = self.artifact_fixture(folder)
            with override_settings(PACKAGE_STORAGE_PATH=folder, ZENPLUS_RELEASE_PUBLIC_KEY=public):
                self.assertEqual(validate_rollout_path(self.releases[-1], {}), [r.version for r in self.releases])
                r = self.releases[1]
                r.min_version = "1.23.6"
                r.save()
                with self.assertRaises(ValueError):
                    validate_rollout_path(self.releases[-1], {})

    def test_promotion_rejects_expired_bridge(self):
        import tempfile
        from django.test import override_settings
        from ota.rollout_guard import validate_rollout_path
        with tempfile.TemporaryDirectory() as folder:
            public = self.artifact_fixture(folder, expire=True)
            with override_settings(PACKAGE_STORAGE_PATH=folder, ZENPLUS_RELEASE_PUBLIC_KEY=public):
                with self.assertRaisesMessage(ValueError, "age window"):
                    validate_rollout_path(self.releases[-1], {})

    def test_promotion_rejects_paused_or_tampered_bridge(self):
        import tempfile
        from pathlib import Path
        from django.test import override_settings
        from ota.rollout_guard import validate_rollout_path
        with tempfile.TemporaryDirectory() as folder:
            public = self.artifact_fixture(folder)
            with override_settings(PACKAGE_STORAGE_PATH=folder, ZENPLUS_RELEASE_PUBLIC_KEY=public):
                RolloutPolicy.objects.create(release=self.releases[1], stage="paused", target_pct=100)
                with self.assertRaisesMessage(ValueError, "no active rollout"):
                    validate_rollout_path(self.releases[-1], {})
                (Path(folder) / "update-1.23.7.zup").write_bytes(b"modified")
                with self.assertRaisesMessage(ValueError, "hash mismatch"):
                    validate_rollout_path(self.releases[-1], {})

    def test_automatic_promotion_stops_on_unverified_path(self):
        from datetime import timedelta
        from unittest.mock import patch
        from ota.tasks import check_rollout_health
        rollout = RolloutPolicy.objects.create(release=self.releases[-1], stage="canary", target_pct=100,
            target_group="stable", auto_promote=True, promote_after=timedelta(seconds=1))
        RolloutPolicy.objects.filter(pk=rollout.pk).update(started_at=timezone.now()-timedelta(hours=2))
        before = RolloutPolicy.objects.count()
        stats = dict(deployed=0, failure_rate=0, pending=0, failed=0)
        with patch("ota.tasks.get_rollout_stats", return_value=stats), patch("ota.rollout_guard.validate_rollout_path", side_effect=ValueError("bridge unavailable")):
            check_rollout_health()
        self.assertEqual(RolloutPolicy.objects.count(), before)
        rollout.refresh_from_db()
        self.assertIsNone(rollout.completed_at)
