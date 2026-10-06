"""Release builds must name their source, refuse dirty trees, and sign a payload
inventory that the appliance-side verifier accepts."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import release_source
from updater import package_trust
from updater.crypto import SecurityError


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / ".gitignore").write_text("dist/\n")
    (path / "app.py").write_text("print('hi')\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


# ── provenance ───────────────────────────────────────────────────────────────

def test_clean_checkout_is_reported_with_its_commit(repo):
    prov = release_source.git_provenance(repo)
    assert prov["commit"] == git(repo, "rev-parse", "HEAD")
    assert prov["branch"] == "main" and prov["dirty"] is False and prov["tag"] is None
    assert release_source.source_problems(prov, allow_dirty=False, require_tag=False) == []


def test_modified_tracked_file_is_dirty(repo):
    (repo / "app.py").write_text("print('changed')\n")
    prov = release_source.git_provenance(repo)
    assert prov["dirty"] is True and prov["dirty_paths"] == ["app.py"]
    problems = release_source.source_problems(prov, allow_dirty=False, require_tag=False)
    assert len(problems) == 1 and "uncommitted" in problems[0]


def test_untracked_file_is_dirty_but_ignored_output_is_not(repo):
    (repo / "dist").mkdir()
    (repo / "dist" / "bundle.js").write_text("built")
    assert release_source.git_provenance(repo)["dirty"] is False
    (repo / "notes.txt").write_text("scratch")
    assert release_source.git_provenance(repo)["dirty"] is True


def test_allow_dirty_permits_the_build_but_the_manifest_says_so(repo):
    (repo / "app.py").write_text("print('changed')\n")
    prov = release_source.git_provenance(repo)
    assert release_source.source_problems(prov, allow_dirty=True, require_tag=False) == []
    assert release_source.manifest_source(prov)["dirty"] is True


def test_a_directory_that_is_not_a_checkout_is_refused(tmp_path):
    prov = release_source.git_provenance(tmp_path)
    assert prov["commit"] is None
    assert "not a readable git checkout" in release_source.source_problems(
        prov, allow_dirty=False, require_tag=False)[0]
    assert release_source.source_problems(prov, allow_dirty=True, require_tag=False) == []


def test_require_tag(repo):
    prov = release_source.git_provenance(repo)
    assert "git tag" in release_source.source_problems(prov, allow_dirty=False, require_tag=True)[0]
    git(repo, "tag", "v1.2.3")
    prov = release_source.git_provenance(repo)
    assert prov["tag"] == "v1.2.3"
    assert release_source.source_problems(prov, allow_dirty=False, require_tag=True) == []


def test_detached_head_has_no_branch(repo):
    git(repo, "checkout", "-q", "--detach")
    prov = release_source.git_provenance(repo)
    assert prov["branch"] is None and prov["commit"]


# ── inventory ────────────────────────────────────────────────────────────────

def make_build_dir(tmp_path: Path) -> Path:
    build = tmp_path / "build"
    (build / "code" / "scripts").mkdir(parents=True)
    (build / "code" / ".version").write_text("1.2.3\n")
    (build / "code" / "scripts" / "run.sh").write_text("#!/bin/sh\n")
    # A nested file that happens to share a reserved name is still payload.
    (build / "code" / "manifest.json").write_text("{}")
    (build / "requirements.txt").write_text("httpx\n")
    (build / "manifest.json").write_text("{}")
    (build / "manifest.json.sig").write_bytes(b"sig")
    (build / "checksums.sha256").write_text("")
    return build


def test_inventory_lists_payload_but_not_the_reserved_top_level_files(tmp_path):
    inventory = release_source.payload_inventory(make_build_dir(tmp_path))
    assert set(inventory) == {
        "code/.version", "code/scripts/run.sh", "code/manifest.json", "requirements.txt"}
    assert all(len(v) == 64 for v in inventory.values())


def test_inventory_refuses_a_symlink(tmp_path):
    build = make_build_dir(tmp_path)
    (build / "code" / "link").symlink_to("/etc")
    with pytest.raises(RuntimeError, match="symlink"):
        release_source.payload_inventory(build)


def test_builder_and_agent_share_the_inventory_definition():
    spec = importlib.util.spec_from_file_location("build_release", ROOT / "scripts" / "build-release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.INVENTORY_KEY == package_trust.INVENTORY_KEY == "payload_files"
    assert set(module.INVENTORY_EXCLUDED) == set(package_trust.INVENTORY_EXCLUDED)


def test_a_package_assembled_like_the_builder_verifies_on_the_appliance(tmp_path, repo):
    """Build side to appliance side: same path spelling, same exclusions."""
    build = make_build_dir(tmp_path)
    key = Ed25519PrivateKey.generate()
    pub = tmp_path / "release.pub"
    pub.write_bytes(key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))

    manifest = {
        "format_version": 2, "version": "1.2.3", "steps": [],
        "release_date": datetime.now(timezone.utc).isoformat(),
        package_trust.INVENTORY_KEY: release_source.payload_inventory(build),
        "source": release_source.manifest_source(release_source.git_provenance(repo)),
    }
    data = json.dumps(manifest, indent=2).encode()
    (build / "manifest.json").write_bytes(data)
    (build / "manifest.json.sig").write_bytes(key.sign(data))

    def public_member(member: tarfile.TarInfo) -> tarfile.TarInfo:
        member.mode = 0o755 if member.isdir() or member.mode & 0o111 else 0o644
        member.uid = member.gid = 0
        return member

    zup = tmp_path / "update.zup"
    with tarfile.open(zup, "w:gz") as tar:  # exactly how build_package() packs it
        for item in sorted(build.iterdir()):
            tar.add(str(item), arcname=item.name, filter=public_member)

    out = tmp_path / "extract"
    verified = package_trust.verify_and_extract(
        str(zup), str(out), public_key_paths=[str(pub)], expected_version="1.2.3")
    assert verified["source"]["dirty"] is False and verified["source"]["commit"]
    assert (out / "code" / "scripts" / "run.sh").read_text() == "#!/bin/sh\n"

    # One byte changed after signing is caught.
    (build / "requirements.txt").write_text("httpx-evil\n")
    tampered = tmp_path / "tampered.zup"
    with tarfile.open(tampered, "w:gz") as tar:
        for item in sorted(build.iterdir()):
            tar.add(str(item), arcname=item.name, filter=public_member)
    with pytest.raises(SecurityError, match="Hash mismatch"):
        package_trust.verify_and_extract(
            str(tampered), str(tmp_path / "extract2"), public_key_paths=[str(pub)])


# ── the real command lines, from a directory that is not the repo ────────────

@pytest.mark.parametrize("script,args", [
    ("build-release.py", ["--help"]),
    ("build-release.py", ["build", "--help"]),
    ("verify-ota-release.py", ["--help"]),
])
def test_release_tools_start_from_any_directory(tmp_path, script, args):
    """A relative-import mistake only shows when the script is run on its own,
    without pytest having put the repository root on sys.path."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *args],
        cwd=tmp_path, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert "--allow-dirty" in result.stdout or "--allow-dirty-source" in result.stdout or "usage" in result.stdout
