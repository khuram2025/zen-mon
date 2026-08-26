from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_builder():
    script = Path(__file__).resolve().parents[2] / "scripts" / "build-release.py"
    spec = importlib.util.spec_from_file_location("zenplus_build_release_publish", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _package(path: Path, *, signed: bool = True, min_version: str | None = None) -> None:
    with tarfile.open(path, "w:gz") as archive:
        manifest = json.dumps({
            "version": "1.21.0",
            "min_version": min_version,
        }).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
        if signed:
            signature = b"signed-manifest"
            info = tarfile.TarInfo("manifest.json.sig")
            info.size = len(signature)
            archive.addfile(info, io.BytesIO(signature))


class _Response:
    def __init__(self, status_code: int, data=None, text: str = ""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        return self._data


class _Client:
    def __init__(
        self,
        *,
        publish_status: int = 200,
        catalog_published: bool = True,
        catalog_id: str = "release-1",
        catalog_version: str = "1.21.0",
        catalog_hash: str | None = None,
    ):
        self.publish_status = publish_status
        self.catalog_published = catalog_published
        self.catalog_id = catalog_id
        self.catalog_version = catalog_version
        self.catalog_hash = catalog_hash
        self.upload_fields = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, **kwargs):
        if url.endswith("/releases/create"):
            self.upload_fields = kwargs["data"]
            return _Response(201, {"id": "release-1"})
        if url.endswith("/releases/release-1/publish"):
            return _Response(self.publish_status, {}, "publish failed")
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, **_kwargs):
        assert url.endswith("/releases")
        return _Response(
            200,
            [{
                "id": self.catalog_id,
                "version": self.catalog_version,
                "package_sha256": (
                    self.catalog_hash
                    if self.catalog_hash is not None
                    else self.upload_fields["package_sha256"]
                ),
                "is_published": self.catalog_published,
            }],
        )


def _install_httpx(monkeypatch, client: _Client) -> None:
    module = SimpleNamespace(
        Client=lambda **_kwargs: client,
        Timeout=lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setitem(sys.modules, "httpx", module)


def test_dashboard_release_build_reinstalls_and_audits_locked_dependencies():
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "build-release.py"
    ).read_text(encoding="utf-8")

    install = '["npm", "ci"]'
    audit = '["npm", "audit", "--omit=dev", "--audit-level=high"]'
    build = '["npx", "--no-install", "vite", "build"]'
    assert install in source
    assert audit in source
    assert build in source
    assert source.index(install) < source.index(audit) < source.index(build)


def test_release_inputs_require_matching_version_and_signing_key(tmp_path, monkeypatch):
    builder = _load_builder()
    (tmp_path / ".version").write_text("1.21.0\n2026-08-26T00:00:00Z\n")
    key = tmp_path / "release.key"
    key.write_bytes(b"test-key-placeholder")
    monkeypatch.setattr(builder, "ZENPLUS_DIR", tmp_path)
    monkeypatch.setattr(builder, "PRIVATE_KEY_PATH", key)

    builder.validate_release_inputs("1.21.0")

    with pytest.raises(RuntimeError, match="does not match"):
        builder.validate_release_inputs("1.20.3")
    key.unlink()
    with pytest.raises(RuntimeError, match="signing key"):
        builder.validate_release_inputs("1.21.0")


def test_release_verifier_requires_explicit_agent_or_appliance_scope(
    tmp_path, monkeypatch
):
    builder = _load_builder()
    key = tmp_path / "release.pub"
    key.write_bytes(b"public-key-placeholder")
    verifier = tmp_path / "scripts" / "verify-ota-release.py"
    verifier.parent.mkdir()
    verifier.write_text("# verifier placeholder\n", encoding="utf-8")
    package = tmp_path / "release.zup"
    package.write_bytes(b"package-placeholder")
    calls = []

    monkeypatch.setattr(builder, "ZENPLUS_DIR", tmp_path)
    monkeypatch.setattr(builder, "PUBLIC_KEY_PATH", key)
    monkeypatch.setattr(builder, "_agent_source_version", lambda: "1.12.4")
    monkeypatch.setattr(
        builder.subprocess,
        "run",
        lambda args, check: calls.append((args, check)),
    )

    builder.verify_release_package(package, "1.20.4")
    builder.verify_release_package(package, "1.20.4", appliance_only=True)

    assert calls[0][0][-2:] == ["--agent-version", "1.12.4"]
    assert calls[1][0][-1] == "--appliance-only"
    assert "--agent-version" not in calls[1][0]
    assert all(check is True for _args, check in calls)


def test_release_script_propagates_verified_appliance_only_scope():
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "release.sh"
    ).read_text(encoding="utf-8")

    assert 'RELEASE_SCOPE="${6:-${ZENPLUS_RELEASE_SCOPE:-bundled}}"' in source
    assert "VERIFY_ARGS+=(--appliance-only)" in source
    assert "BUILD_ARGS+=(--skip-agent-artifacts)" in source
    assert "PUBLISH_ARGS+=(--skip-agent-artifacts)" in source


def test_publish_fails_before_authentication_when_package_is_unsigned(
    tmp_path, monkeypatch
):
    builder = _load_builder()
    package = tmp_path / "unsigned.zup"
    _package(package, signed=False)
    monkeypatch.setattr(
        builder,
        "get_admin_token",
        lambda: pytest.fail("authentication must not run for an unsigned package"),
    )

    with pytest.raises(SystemExit):
        builder.publish_package(package, "1.21.0", "features", "normal", None)


def test_publish_sends_min_version_and_confirms_catalog_state(tmp_path, monkeypatch):
    builder = _load_builder()
    package = tmp_path / "signed.zup"
    _package(package, min_version="1.20.2")
    client = _Client()
    _install_httpx(monkeypatch, client)
    monkeypatch.setattr(builder, "get_admin_token", lambda: "token")
    monkeypatch.setattr(builder, "verify_release_package", lambda *_args: None)

    builder.publish_package(package, "1.21.0", "features", "normal", "1.20.2")

    assert client.upload_fields["min_version"] == "1.20.2"


def test_publish_rejects_min_version_that_differs_from_signed_manifest(
    tmp_path, monkeypatch
):
    builder = _load_builder()
    package = tmp_path / "signed.zup"
    _package(package, min_version="1.20.2")
    monkeypatch.setattr(
        builder,
        "get_admin_token",
        lambda: pytest.fail("authentication must not run for mismatched metadata"),
    )

    with pytest.raises(SystemExit):
        builder.publish_package(package, "1.21.0", "features", "normal", None)


@pytest.mark.parametrize(
    (
        "publish_status",
        "catalog_published",
        "catalog_id",
        "catalog_version",
        "catalog_hash",
    ),
    [
        (500, True, "release-1", "1.21.0", None),
        (200, False, "release-1", "1.21.0", None),
        (200, True, "older-release", "1.21.0", None),
        (200, True, "release-1", "1.20.2", None),
        (200, True, "release-1", "1.21.0", "wrong-hash"),
    ],
)
def test_publish_never_reports_success_without_confirmed_publication(
    tmp_path,
    monkeypatch,
    capsys,
    publish_status,
    catalog_published,
    catalog_id,
    catalog_version,
    catalog_hash,
):
    builder = _load_builder()
    package = tmp_path / "signed.zup"
    _package(package)
    client = _Client(
        publish_status=publish_status,
        catalog_published=catalog_published,
        catalog_id=catalog_id,
        catalog_version=catalog_version,
        catalog_hash=catalog_hash,
    )
    _install_httpx(monkeypatch, client)
    monkeypatch.setattr(builder, "get_admin_token", lambda: "token")
    monkeypatch.setattr(builder, "verify_release_package", lambda *_args: None)

    with pytest.raises(SystemExit):
        builder.publish_package(package, "1.21.0", "features", "normal", None)

    assert "is live" not in capsys.readouterr().out
