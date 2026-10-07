"""resign-release.py must turn a lab-tested package into a production package
without changing a byte of what was tested, and must refuse anything that was
not fully verified first."""

from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from updater import package_trust
from updater.crypto import SecurityError

spec = importlib.util.spec_from_file_location("resign_release", ROOT / "scripts" / "resign-release.py")
resign_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resign_release)
verify_spec = importlib.util.spec_from_file_location('verify_ota', ROOT / 'scripts' / 'verify-ota-release.py')
verify_ota = importlib.util.module_from_spec(verify_spec)
verify_spec.loader.exec_module(verify_ota)


class Keys:
    def __init__(self, directory: Path, name: str):
        self.private = Ed25519PrivateKey.generate()
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)
        self.pub = directory / f"{name}.pub"
        self.pub.write_bytes(self.private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
        self.pub.chmod(0o644)
        self.key = directory / f"{name}.key"
        self.key.write_bytes(self.private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        self.key.chmod(0o600)


FILES = {
    "code/.version": b"1.2.3\n",
    "code/scripts/run.sh": b"#!/bin/sh\n",
    "requirements.txt": b"httpx\n",
}


def make_package(path: Path, keys: Keys, *, source=None, files=None, inventory_override=None):
    import hashlib
    files = FILES if files is None else files
    manifest = {
        "version": "1.2.3", "steps": [],
        "release_date": datetime.now(timezone.utc).isoformat(),
        "payload_files": inventory_override or {n: hashlib.sha256(d).hexdigest() for n, d in FILES.items()},
        "source": {"commit": "a" * 40, "branch": "main", "tag": "v1.2.3", "dirty": False} if source is None else source,
    }
    data = json.dumps(manifest, indent=2).encode()
    with tarfile.open(path, "w:gz") as tar:
        def add(name, content):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
        add("manifest.json", data)
        add("manifest.json.sig", keys.private.sign(data))
        for name, content in files.items():
            add(name, content)
    return data


@pytest.fixture
def world(tmp_path):
    test, prod = Keys(tmp_path / "keys", "test"), Keys(tmp_path / "keys", "prod")
    pkg = tmp_path / "update.zup"
    manifest = make_package(pkg, test)
    return {"test": test, "prod": prod, "pkg": pkg, "out": tmp_path / "final" / "update.zup",
            "manifest": manifest, "tmp": tmp_path}


def resign(w, **kw):
    kw.setdefault("test_public_keys", [str(w["test"].pub)])
    kw.setdefault("signing_key", w["prod"].key)
    return resign_release.resign(w["pkg"], w["out"], **kw)


def read_members(path: Path) -> dict[str, bytes]:
    with tarfile.open(path, "r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isreg()}


def test_the_resigned_package_installs_under_the_production_key_only(world, tmp_path):
    summary = resign(world)
    assert summary["version"] == "1.2.3" and summary["source_commit"] == "a" * 40
    package_trust.verify_and_extract(str(world["out"]), str(tmp_path / "x1"),
                                     public_key_paths=[str(world["prod"].pub)])
    with pytest.raises(SecurityError, match="signature"):
        package_trust.verify_and_extract(str(world["out"]), str(tmp_path / "x2"),
                                         public_key_paths=[str(world["test"].pub)])


def test_the_lab_package_is_not_accepted_by_production_appliances(world, tmp_path):
    with pytest.raises(SecurityError, match="signature"):
        package_trust.verify_and_extract(str(world["pkg"]), str(tmp_path / "x"),
                                         public_key_paths=[str(world["prod"].pub)])


def test_everything_except_the_signature_is_identical_to_what_was_tested(world):
    resign(world)
    before, after = read_members(world["pkg"]), read_members(world["out"])
    assert set(before) == set(after)
    assert after["manifest.json"] == before["manifest.json"] == world["manifest"]
    for name in before:
        if name != "manifest.json.sig":
            assert after[name] == before[name], name
    assert after["manifest.json.sig"] != before["manifest.json.sig"]


def test_the_original_package_is_left_untouched(world):
    original = world["pkg"].read_bytes()
    resign(world)
    assert world["pkg"].read_bytes() == original


def test_it_will_not_overwrite_an_existing_artifact(world):
    world["out"].parent.mkdir(parents=True)
    world["out"].write_bytes(b"already published")
    with pytest.raises(SecurityError, match="already exists"):
        resign(world)
    assert world["out"].read_bytes() == b"already published"


def test_a_package_not_signed_by_the_named_test_key_is_refused(world, tmp_path):
    stranger = Keys(tmp_path / "other", "stranger")
    with pytest.raises(SecurityError, match="signature"):
        resign(world, test_public_keys=[str(stranger.pub)])
    assert not world["out"].exists()


def test_a_tampered_payload_is_refused_even_though_the_signature_is_valid(world):
    # The manifest (and its valid signature) still lists the original requirements.txt.
    make_package(world["pkg"], world["test"], files=dict(FILES, **{"requirements.txt": b"evil\n"}))
    with pytest.raises(SecurityError, match="Hash mismatch"):
        resign(world)
    assert not world["out"].exists()


@pytest.mark.parametrize("source", [
    {"commit": "a" * 40, "dirty": True},
    {"commit": None, "dirty": False},
    {},
])
def test_a_package_from_dirty_or_unknown_source_is_refused(world, source):
    make_package(world["pkg"], world["test"], source=source)
    with pytest.raises(SecurityError, match="clean, committed source"):
        resign(world)
    assert not world["out"].exists()


def test_dirty_source_can_be_signed_only_with_the_explicit_lab_flag(world):
    make_package(world["pkg"], world["test"], source={"commit": "a" * 40, "dirty": True})
    resign(world, allow_dirty_source=True)
    assert world["out"].exists()


def test_the_command_line_works_and_reports_the_new_hash(world):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "resign-release.py"), str(world["pkg"]),
         "--test-public-key", str(world["test"].pub), "--signing-key", str(world["prod"].key),
         "--output", str(world["out"])],
        cwd=world["tmp"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["output_sha256"] and Path(summary["output"]) == world["out"]


def test_the_command_line_reports_a_refusal_without_a_traceback(world, tmp_path):
    stranger = Keys(tmp_path / "other", "stranger")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "resign-release.py"), str(world["pkg"]),
         "--test-public-key", str(stranger.pub), "--signing-key", str(world["prod"].key),
         "--output", str(world["out"])],
        cwd=world["tmp"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 1
    assert result.stderr.startswith("ERROR:") and "Traceback" not in result.stderr


def sensor_files(keys, *, bad_signature=False, bad_hash=False):
    binary = b'fixture sensor binary\n'
    metadata = json.dumps({'binary': 'zenplus-sensor',
                           'sha256': '0' * 64 if bad_hash else hashlib.sha256(binary).hexdigest()}).encode()
    prefix = 'sensor-artifacts/bin/linux-amd64/'
    return {prefix + 'zenplus-sensor': binary, prefix + 'manifest.json': metadata,
            prefix + 'manifest.json.sig': b'x' * 64 if bad_signature else keys.private.sign(metadata)}


def test_sensor_signature_is_promoted_and_inventory_checksums_follow(world, tmp_path):
    files = dict(FILES, **sensor_files(world['test']))
    inventory = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    before_manifest = make_package(world['pkg'], world['test'], files=files, inventory_override=inventory)
    checksum_files = dict(files, **{'manifest.json': before_manifest})
    checksums = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n'
                        for name, data in checksum_files.items()).encode()
    make_package(world['pkg'], world['test'], files=dict(files, **{'checksums.sha256': checksums}),
                 inventory_override=inventory)
    summary = resign(world)
    before, after = read_members(world['pkg']), read_members(world['out'])
    prefix = 'sensor-artifacts/bin/linux-amd64/'
    world['prod'].private.public_key().verify(after[prefix + 'manifest.json.sig'], after[prefix + 'manifest.json'])
    assert after[prefix + 'manifest.json'] == before[prefix + 'manifest.json']
    assert after[prefix + 'zenplus-sensor'] == before[prefix + 'zenplus-sensor']
    assert summary['nested_signatures_changed'] == [prefix + 'manifest.json.sig']
    assert summary['manifest_unchanged'] is False
    with tarfile.open(world['pkg'], 'r:gz') as tar:
        with pytest.raises(RuntimeError, match='sensor release signature'):
            verify_ota.verify_sensor_manifests(tar, world['prod'].private.public_key())
    with tarfile.open(world['out'], 'r:gz') as tar:
        assert verify_ota.verify_sensor_manifests(tar, world['prod'].private.public_key()) == 1
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {prefix + 'manifest.json.sig', 'manifest.json', 'manifest.json.sig', 'checksums.sha256'}
    package_trust.verify_and_extract(str(world['out']), str(tmp_path / 'verified'),
                                     public_key_paths=[str(world['prod'].pub)])
    for line in after['checksums.sha256'].decode().splitlines():
        digest, name = line.split('  ', 1)
        assert hashlib.sha256(after[name]).hexdigest() == digest


@pytest.mark.parametrize('bad', ['signature', 'hash'])
def test_invalid_sensor_metadata_is_not_promoted(world, bad):
    files = dict(FILES, **sensor_files(world['test'], bad_signature=bad == 'signature', bad_hash=bad == 'hash'))
    make_package(world['pkg'], world['test'], files=files,
                 inventory_override={name: hashlib.sha256(data).hexdigest() for name, data in files.items()})
    with pytest.raises(SecurityError, match='sensor'):
        resign(world)
    assert not world['out'].exists()


def test_repacking_uses_verified_snapshot_when_input_changes(world, monkeypatch):
    original_verify = resign_release.verify_and_extract
    def replace_after_verify(*args, **kwargs):
        manifest = original_verify(*args, **kwargs)
        make_package(world['pkg'], world['test'], files=dict(FILES, **{'requirements.txt': b'changed after verification'}))
        return manifest
    monkeypatch.setattr(resign_release, 'verify_and_extract', replace_after_verify)
    resign(world)
    assert read_members(world['out'])['requirements.txt'] == FILES['requirements.txt']


def test_preexisting_predictable_partial_symlink_is_not_followed(world):
    world['out'].parent.mkdir(parents=True)
    victim = world['tmp'] / 'victim'
    victim.write_bytes(b'keep this')
    world['out'].with_name(world['out'].name + '.partial').symlink_to(victim)
    resign(world)
    assert victim.read_bytes() == b'keep this'
