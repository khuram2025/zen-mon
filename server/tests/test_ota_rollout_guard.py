"""The portal's rollout promotion guard (integrations/zentryc_ota/rollout_guard.py), run without Django.

The guard verifies real signed packages, so these tests build real Ed25519-signed archives and stand in only for the
Django pieces it imports (settings, the Release model's query and the rollout-policy relation).
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Chain of the real portal: 1.23.7 is the entry, every later release names its predecessor as its minimum.
CHAIN = [("1.23.7", None), ("1.23.8", "1.23.7"), ("1.23.9", "1.23.8"), ("1.24.0", "1.23.9")]


class Policies:
    def __init__(self, items):
        self.items = items

    def order_by(self, *_):
        return self

    def first(self):
        return self.items[-1] if self.items else None


@pytest.fixture
def guard(monkeypatch, tmp_path):
    """Import the guard with a stand-in for Django and the `ota` package; return (module, settings, releases, helpers)."""
    from integrations.zentryc_ota import upgrade_path

    settings = NS(BASE_DIR=tmp_path, PACKAGE_STORAGE_PATH=tmp_path, ZENPLUS_RELEASE_PUBLIC_KEY=tmp_path / "release.pub")
    releases: list = []

    class Release:
        class objects:  # noqa: N801 - mirrors the Django manager access
            @staticmethod
            def filter(**kwargs):
                return [r for r in releases if r.product == kwargs["product"] and r.arch == kwargs["arch"] and r.is_published]

    django = types.ModuleType("django")
    conf = types.ModuleType("django.conf")
    conf.settings = settings
    ota = types.ModuleType("ota")
    models = types.ModuleType("ota.models")
    models.Release = Release
    ota_path = types.ModuleType("ota.upgrade_path")
    ota_path.plan_upgrade = upgrade_path.plan_upgrade
    for name, module in (("django", django), ("django.conf", conf), ("ota", ota), ("ota.models", models),
                         ("ota.upgrade_path", ota_path)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "integrations.zentryc_ota.rollout_guard", raising=False)
    from integrations.zentryc_ota import rollout_guard

    key = Ed25519PrivateKey.generate()
    (tmp_path / "release.pub").write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))

    def build(*, old_age_days=0, target_age_days=0, chain=CHAIN):
        """Sign and store one package per release; the target is the last one."""
        releases.clear()
        for index, (version, minimum) in enumerate(chain):
            target = index == len(chain) - 1
            age = target_age_days if target else old_age_days
            manifest = json.dumps(dict(version=version, min_version=minimum, arch="amd64",
                                       release_date=(datetime.now(timezone.utc) - timedelta(days=age)).isoformat())).encode()
            package = tmp_path / f"update-{version}.zup"
            with tarfile.open(package, "w:gz") as archive:
                for name, data in (("manifest.json", manifest), ("manifest.json.sig", key.sign(manifest))):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            releases.append(NS(pk=version, version=version, min_version=minimum, arch="amd64", product="ota", is_published=True,
                               package_sha256=hashlib.sha256(package.read_bytes()).hexdigest(),
                               rollout_policies=Policies([NS(stage="full", target_pct=100, target_group=None, completed_at=None)])))
        return releases[-1]

    return NS(module=rollout_guard, settings=settings, releases=releases, build=build, dir=tmp_path, key=key)


def test_ageing_old_bridges_do_not_block_a_fresh_release(guard):
    target = guard.build(old_age_days=31)          # 1.23.7 to 1.23.9 are all past the 30-day window, 1.24.0 is fresh
    assert guard.module.validate_rollout_path(target, {}) == ["1.24.0"]


def test_the_whole_chain_can_still_be_demanded(guard):
    target = guard.build(old_age_days=31)
    guard.settings.ZENPLUS_OTA_VALIDATE_FULL_CHAIN = True
    with pytest.raises(ValueError, match="age window"):
        guard.module.validate_rollout_path(target, {})
    guard.build(old_age_days=0)                    # a fresh chain passes in full-chain mode, in install order
    assert guard.module.validate_rollout_path(guard.releases[-1], {}) == ["1.23.7", "1.23.8", "1.23.9", "1.24.0"]


def test_the_promoted_release_must_itself_be_inside_the_window(guard):
    target = guard.build(old_age_days=0, target_age_days=31)
    with pytest.raises(ValueError, match="age window"):
        guard.module.validate_rollout_path(target, {})


def test_a_future_dated_release_is_refused(guard):
    target = guard.build(target_age_days=-3)
    with pytest.raises(ValueError, match="age window"):
        guard.module.validate_rollout_path(target, {})


def test_a_modified_package_is_refused(guard):
    target = guard.build()
    (guard.dir / "update-1.24.0.zup").write_bytes(b"modified")
    with pytest.raises(ValueError, match="hash mismatch"):
        guard.module.validate_rollout_path(target, {})


def test_a_package_signed_by_another_key_is_refused(guard):
    target = guard.build()
    other = Ed25519PrivateKey.generate()
    (guard.dir / "release.pub").write_bytes(other.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    with pytest.raises(Exception):                  # cryptography.exceptions.InvalidSignature
        guard.module.validate_rollout_path(target, {})


def test_a_catalog_minimum_that_differs_from_the_signed_one_is_refused(guard):
    target = guard.build()
    target.min_version = "1.23.8"
    with pytest.raises(ValueError, match="min_version differs"):
        guard.module.validate_rollout_path(target, {})


def test_a_release_without_a_minimum_is_checked_on_its_own(guard):
    target = guard.build(chain=[("1.23.7", None)])
    assert guard.module.validate_rollout_path(target, {}) == ["1.23.7"]


def test_full_chain_mode_still_requires_every_bridge_to_be_rolled_out_at_one_hundred_percent(guard):
    target = guard.build()
    guard.settings.ZENPLUS_OTA_VALIDATE_FULL_CHAIN = True
    guard.releases[1].rollout_policies = Policies([NS(stage="paused", target_pct=100, target_group=None, completed_at=None)])
    with pytest.raises(ValueError, match="no active rollout"):
        guard.module.validate_rollout_path(target, {})
    guard.releases[1].rollout_policies = Policies([NS(stage="full", target_pct=50, target_group=None, completed_at=None)])
    with pytest.raises(ValueError, match="100%"):
        guard.module.validate_rollout_path(target, {})


def test_an_unpublished_minimum_is_still_an_error_in_full_chain_mode_only(guard):
    target = guard.build()
    guard.releases[2].is_published = False         # 1.23.9, the signed minimum of 1.24.0
    assert guard.module.validate_rollout_path(target, {}) == ["1.24.0"]   # appliances at 1.23.9 need no bridge
    guard.settings.ZENPLUS_OTA_VALIDATE_FULL_CHAIN = True
    with pytest.raises(Exception, match="missing or unpublished"):
        guard.module.validate_rollout_path(target, {})
