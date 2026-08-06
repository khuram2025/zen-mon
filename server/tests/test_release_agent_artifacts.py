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


def test_stage_agent_artifacts_requires_msi_matching_source(tmp_path, monkeypatch):
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


def test_stage_agent_artifacts_copies_current_msi_and_writes_manifest(tmp_path, monkeypatch):
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


def test_stage_agent_artifacts_rejects_truncated_msi(tmp_path, monkeypatch):
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


# ─── Agent source not vendored here ───────────────────────────────────────────


def test_newest_msi_ships_when_the_agent_source_is_not_vendored(tmp_path, monkeypatch):
    """The agent lives in its own repo. Without its source there is nothing to
    check a version against, so the newest package in the store is shipped."""
    builder = _load_builder()
    repo = tmp_path / "repo"          # no ZenPlus_Agent/ inside
    repo.mkdir()
    artifacts = tmp_path / "artifacts"
    build = tmp_path / "build"
    build.mkdir()
    _write_sparse_package(artifacts / "windows" / "zenplus-agent-1.3.2.msi")
    newest = artifacts / "windows" / "zenplus-agent-1.5.2.msi"
    with newest.open("wb") as package:
        package.seek(1_000_001)
        package.write(b"\0")
    monkeypatch.setattr(builder, "ZENPLUS_DIR", repo)

    staged = builder.stage_agent_artifacts(build, artifacts)

    assert [p["version"] for p in staged] == ["1.5.2"]
    manifest = json.loads((build / "agent-artifacts" / "manifest.json").read_text())
    assert manifest["required_windows_version"] == "1.5.2"
    assert (build / "agent-artifacts" / "windows" / newest.name).is_file()


def test_missing_msi_still_fails_when_the_source_is_not_vendored(tmp_path, monkeypatch):
    """Dropping the version contract must not also drop the artifact check —
    a release with no MSI regresses appliances to 'no package published'."""
    builder = _load_builder()
    repo = tmp_path / "repo"
    repo.mkdir()
    artifacts = tmp_path / "artifacts"
    (artifacts / "windows").mkdir(parents=True)
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(builder, "ZENPLUS_DIR", repo)

    with pytest.raises(RuntimeError, match="No Windows agent MSI"):
        builder.stage_agent_artifacts(build, artifacts)


def test_missing_msi_is_a_warning_when_not_required(tmp_path, monkeypatch, capsys):
    builder = _load_builder()
    repo = tmp_path / "repo"
    repo.mkdir()
    artifacts = tmp_path / "artifacts"
    (artifacts / "windows").mkdir(parents=True)
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(builder, "ZENPLUS_DIR", repo)

    assert builder.stage_agent_artifacts(build, artifacts, required=False) == []
    assert "No Windows agent MSI" in capsys.readouterr().out


def test_agent_source_version_is_none_when_not_vendored(tmp_path, monkeypatch):
    builder = _load_builder()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(builder, "ZENPLUS_DIR", repo)

    assert builder._agent_source_version() is None
