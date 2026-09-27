from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load_builder():
    script = Path(__file__).resolve().parents[2] / "scripts" / "build-release.py"
    spec = importlib.util.spec_from_file_location("zenplus_build_release", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_agent_source(root: Path, version: str) -> None:
    model = root / "ZenPlus_Agent" / "internal" / "model" / "model.go"
    model.parent.mkdir(parents=True)
    model.write_text(
        f'package model\n\nconst AgentVersion = "{version}"\n',
        encoding="utf-8",
    )


def _write_sparse_package(path: Path, size: int = 1_000_001) -> None:
    path.parent.mkdir(parents=True)
    with path.open("wb") as package:
        package.seek(size - 1)
        package.write(b"\0")


def test_stage_agent_artifacts_requires_windows_installer_matching_source(tmp_path, monkeypatch):
    builder = _load_builder()
    repo = tmp_path / "repo"
    artifacts = tmp_path / "artifacts"
    build = tmp_path / "build"
    build.mkdir()
    _write_agent_source(repo, "1.3.0")
    _write_sparse_package(artifacts / "windows" / "zenplus-agent-1.2.0.msi")
    monkeypatch.setattr(builder, "ZENPLUS_DIR", repo)

    with pytest.raises(RuntimeError, match="Expected .*zenplus-agent-1.3.0.msi"):
        builder.stage_agent_artifacts(build, artifacts)


def test_stage_agent_artifacts_copies_current_installer_and_writes_manifest(tmp_path, monkeypatch):
    builder = _load_builder()
    repo = tmp_path / "repo"
    artifacts = tmp_path / "artifacts"
    build = tmp_path / "build"
    build.mkdir()
    _write_agent_source(repo, "1.3.0")
    package = artifacts / "windows" / "zenplus-agent-1.3.0.msi"
    _write_sparse_package(package)
    monkeypatch.setattr(builder, "ZENPLUS_DIR", repo)

    staged = builder.stage_agent_artifacts(build, artifacts)

    assert staged == [{
        "platform": "windows",
        "version": "1.3.0",
        "file_name": package.name,
        "file_size": package.stat().st_size,
        "sha256": builder.sha256_file(str(package)),
    }]
    copied = build / "agent-artifacts" / "windows" / package.name
    assert copied.is_file()
    manifest = json.loads((build / "agent-artifacts" / "manifest.json").read_text())
    assert manifest["required_windows_version"] == "1.3.0"
    assert manifest["packages"] == staged


def test_stage_agent_artifacts_rejects_truncated_installer(tmp_path, monkeypatch):
    builder = _load_builder()
    repo = tmp_path / "repo"
    artifacts = tmp_path / "artifacts"
    build = tmp_path / "build"
    build.mkdir()
    _write_agent_source(repo, "1.3.0")
    package = artifacts / "windows" / "zenplus-agent-1.3.0.msi"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"not an installer")
    monkeypatch.setattr(builder, "ZENPLUS_DIR", repo)

    with pytest.raises(RuntimeError, match="looks truncated"):
        builder.stage_agent_artifacts(build, artifacts)


def test_apm_bundle_builder_accepts_go_from_ci_path():
    script = (
        Path(__file__).resolve().parents[2]
        / "ZenPlus_Agent"
        / "scripts"
        / "prepare-apm-bundle.ps1"
    ).read_text(encoding="utf-8")

    assert "Get-Command go.exe" in script
    assert "Get-Command go -ErrorAction SilentlyContinue" in script
    assert 'throw "Go was not found in .tools\\go or PATH"' in script
    assert "Portable Go toolchain is missing" not in script


def test_stage_agent_artifacts_ships_both_windows_flavours(tmp_path, monkeypatch):
    builder = _load_builder()
    repo = tmp_path / "repo"
    artifacts = tmp_path / "artifacts"
    build = tmp_path / "build"
    build.mkdir()
    _write_agent_source(repo, "1.14.2")
    names = [
        "zenplus-agent-1.14.2.msi",
        "zenplus-agent-1.14.2.exe",
        "zenplus-agent-core-1.14.2.exe",
        "zenplus-agent-core-1.14.2.msi",
    ]
    for name in names:
        path = artifacts / "windows" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as package:
            package.seek(1_000_000)
            package.write(b"\0")
    # An older version beside it is not shipped.
    with (artifacts / "windows" / "zenplus-agent-core-1.14.1.exe").open("wb") as old:
        old.seek(1_000_000)
        old.write(b"\0")
    monkeypatch.setattr(builder, "ZENPLUS_DIR", repo)

    staged = builder.stage_agent_artifacts(build, artifacts)

    assert sorted(item["file_name"] for item in staged) == sorted(names)
    assert all(item["version"] == "1.14.2" for item in staged)
    for name in names:
        assert (build / "agent-artifacts" / "windows" / name).is_file()


def test_stage_agent_artifacts_refuses_a_release_missing_a_flavour(tmp_path, monkeypatch):
    builder = _load_builder()
    repo = tmp_path / "repo"
    artifacts = tmp_path / "artifacts"
    build = tmp_path / "build"
    build.mkdir()
    _write_agent_source(repo, "1.14.2")
    for name in ("zenplus-agent-1.14.2.msi", "zenplus-agent-1.14.2.exe"):
        path = artifacts / "windows" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as package:
            package.seek(1_000_000)
            package.write(b"\0")
    monkeypatch.setattr(builder, "ZENPLUS_DIR", repo)

    with pytest.raises(RuntimeError, match="missing zenplus-agent-core-1.14.2.exe"):
        builder.stage_agent_artifacts(build, artifacts)
