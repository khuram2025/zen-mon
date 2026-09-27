"""The signed full MSI can be smaller than older offline bundles."""
import importlib.util
import io
from pathlib import Path
import tarfile

import pytest


def verifier():
    path = Path(__file__).resolve().parents[2] / "scripts/verify-ota-release.py"
    spec = importlib.util.spec_from_file_location("verify_ota_msi", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive(size, header=bytes.fromhex("d0cf11e0a1b11ae1")):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        member = tarfile.TarInfo("agent.msi")
        member.size = size
        tar.addfile(member, io.BytesIO((header + b"\0" * size)[:size]))
    data.seek(0)
    return tarfile.open(fileobj=data)


def test_compact_msi_does_not_require_historical_100mb_floor():
    with archive(1_000_000) as tar:
        assert verifier().verify_msi_container(tar, "agent.msi").size == 1_000_000


@pytest.mark.parametrize("size", [0, 8, 999_999])
def test_truncated_msi_is_rejected(size):
    with archive(size) as tar:
        with pytest.raises(RuntimeError, match="missing or truncated"):
            verifier().verify_msi_container(tar, "agent.msi")


def test_non_msi_content_is_rejected_even_when_large():
    with archive(1_000_000, b"MZnotmsi") as tar:
        with pytest.raises(RuntimeError, match="invalid compound-document header"):
            verifier().verify_msi_container(tar, "agent.msi")


def test_missing_msi_is_rejected():
    with archive(1_000_000) as tar:
        with pytest.raises(RuntimeError, match="missing or truncated"):
            verifier().verify_msi_container(tar, "missing.msi")
