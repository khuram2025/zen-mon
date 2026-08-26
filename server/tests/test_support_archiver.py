from __future__ import annotations

import sys
import tarfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from support.archiver import (  # noqa: E402
    BundleArchive,
    DEFAULT_PER_FILE_CAP,
    DEFAULT_TOTAL_CAP,
    TRUNCATION_MARKER,
)


def _read(tar_path: Path, arcname: str) -> bytes:
    with tarfile.open(tar_path, "r:gz") as tar:
        f = tar.extractfile(arcname)
        assert f is not None
        return f.read()


def test_small_files_are_added_without_truncation(tmp_path):
    out = tmp_path / "bundle.tar.gz"
    with BundleArchive(output_path=out) as arch:
        arch.add("inventory/system.json", b'{"hostname": "node-1"}\n')
        arch.add("logs/journal-zenplus-api.log", b"some short log\n")
        arch.finalize_checksums()

    assert out.exists()
    body = _read(out, "inventory/system.json")
    assert body == b'{"hostname": "node-1"}\n'
    checksums = _read(out, "checksums.sha256").decode("utf-8")
    assert "inventory/system.json" in checksums


def test_per_file_cap_truncates_and_marks(tmp_path):
    out = tmp_path / "bundle.tar.gz"
    big = b"X" * (DEFAULT_PER_FILE_CAP + 1024)
    with BundleArchive(output_path=out, per_file_cap=1024) as arch:
        result = arch.add("logs/huge.log", big)
        arch.finalize_checksums()

    assert result.truncated is True
    body = _read(out, "logs/huge.log")
    assert body.endswith(TRUNCATION_MARKER)
    assert len(body) == 1024


def test_total_cap_skips_overflowing_files(tmp_path):
    """Once the total cap is hit, further adds are recorded as skipped rather
    than overflowing the tarball or hiding the omission."""
    out = tmp_path / "bundle.tar.gz"
    payload = b"Y" * 800  # 800-byte files
    with BundleArchive(output_path=out, per_file_cap=10_000, total_cap=2000) as arch:
        arch.add("a.log", payload)
        arch.add("b.log", payload)
        arch.add("c.log", payload)  # would push to 2400 > 2000 cap
        arch.add("d.log", payload)
        arch.finalize_checksums()

    assert "c.log" in arch.skipped
    assert "d.log" in arch.skipped
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
        assert "a.log" in names and "b.log" in names
        assert "c.log" not in names and "d.log" not in names
        assert "checksums.sha256" in names


def test_tar_round_trips_cleanly(tmp_path):
    out = tmp_path / "bundle.tar.gz"
    files = {f"section/file-{i}.txt": f"hello {i}".encode() for i in range(5)}
    with BundleArchive(output_path=out) as arch:
        for arc, data in files.items():
            arch.add(arc, data)
        arch.finalize_checksums()

    with tarfile.open(out, "r:gz") as tar:
        for arc, data in files.items():
            assert tar.extractfile(arc).read() == data


def test_checksum_file_lists_every_entry_except_itself(tmp_path):
    out = tmp_path / "bundle.tar.gz"
    with BundleArchive(output_path=out) as arch:
        arch.add("a.log", b"a")
        arch.add("b.log", b"bb")
        arch.finalize_checksums()

    body = _read(out, "checksums.sha256").decode("utf-8")
    # Both data files appear, checksums.sha256 itself must not appear.
    assert "a.log" in body and "b.log" in body
    assert "checksums.sha256" not in body


@pytest.mark.parametrize("unsafe", ["../outside", "a/../../b", "/absolute", "a\\b", "a\nb", "C:/drive"])
def test_unsafe_member_names_are_rejected(tmp_path, unsafe):
    out = tmp_path / "bundle.tar.gz"
    with BundleArchive(output_path=out) as arch:
        with pytest.raises(ValueError):
            arch.add(unsafe, b"x")


def test_duplicate_member_names_are_rejected(tmp_path):
    out = tmp_path / "bundle.tar.gz"
    with BundleArchive(output_path=out) as arch:
        arch.add("same.txt", b"one")
        with pytest.raises(ValueError):
            arch.add("same.txt", b"two")


def test_identical_inputs_produce_identical_archive_bytes(tmp_path):
    outputs = []
    for name in ("one.tar.gz", "two.tar.gz"):
        path = tmp_path / name
        with BundleArchive(output_path=path) as arch:
            arch.add("a.txt", b"stable")
            arch.finalize_checksums()
        outputs.append(path.read_bytes())
    assert outputs[0] == outputs[1]


def test_exception_leaves_no_partial_final_archive(tmp_path):
    out = tmp_path / "bundle.tar.gz"
    with pytest.raises(RuntimeError):
        with BundleArchive(output_path=out) as arch:
            arch.add("partial.txt", b"partial")
            raise RuntimeError("boom")
    assert not out.exists()
    assert not list(tmp_path.glob("*.tmp"))
