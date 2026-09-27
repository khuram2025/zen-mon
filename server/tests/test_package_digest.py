from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from app.services import package_digest


@pytest.fixture(autouse=True)
def _fresh_cache():
    package_digest.clear_cache()
    yield
    package_digest.clear_cache()


def _count_reads(monkeypatch) -> list[Path]:
    opened: list[Path] = []
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        opened.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    return opened


def test_unchanged_package_is_hashed_once(tmp_path, monkeypatch):
    package = tmp_path / "zenplus-agent-1.12.9.msi"
    package.write_bytes(b"signed installer bytes" * 1000)
    opened = _count_reads(monkeypatch)

    first = package_digest.sha256_file(package)
    second = package_digest.sha256_file(package)

    assert first == second == hashlib.sha256(package.read_bytes()).hexdigest()
    assert len(opened) == 2  # one hash read plus the explicit read_bytes above


def test_modified_package_is_rehashed_even_with_same_size_and_mtime(tmp_path):
    package = tmp_path / "zenplus-agent-1.12.9.msi"
    package.write_bytes(b"A" * 4096)
    original = package_digest.sha256_file(package)
    st = package.stat()

    # Same length, mtime forced back to the original: only ctime reveals it.
    time.sleep(0.01)
    package.write_bytes(b"B" * 4096)
    os.utime(package, ns=(st.st_atime_ns, st.st_mtime_ns))

    tampered = package_digest.sha256_file(package)
    assert tampered != original
    assert tampered == hashlib.sha256(b"B" * 4096).hexdigest()


def test_replaced_file_is_rehashed(tmp_path):
    package = tmp_path / "zenplus-agent-1.12.9.msi"
    package.write_bytes(b"original")
    package_digest.sha256_file(package)

    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"swapped!")
    os.replace(replacement, package)

    assert package_digest.sha256_file(package) == hashlib.sha256(b"swapped!").hexdigest()
