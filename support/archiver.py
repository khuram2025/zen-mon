"""Tarball builder for support bundles.

Caps:
- per-file content cap (default 5 MiB after redaction) — file body is
  truncated and a clear ``[support-bundle: truncated]`` marker is appended.
- whole-bundle cap (default 50 MiB pre-compression) — once reached, any
  further files are skipped and the manifest section is updated.

Files are added to the tarball as in-memory bytes (already redacted by the
caller). We never tar from on-disk paths directly: that would risk leaking
ownership/mtime metadata or files we didn't redact.
"""

from __future__ import annotations

import hashlib
import gzip
import io
import os
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import PurePosixPath
from typing import BinaryIO


DEFAULT_PER_FILE_CAP = 5 * 1024 * 1024
EXTENDED_PER_FILE_CAP = 15 * 1024 * 1024
DEFAULT_TOTAL_CAP = 50 * 1024 * 1024
DEFAULT_METADATA_RESERVE = 512 * 1024
DEFAULT_ARCHIVE_OVERHEAD_RESERVE = 1024 * 1024
DEFAULT_MAX_ENTRIES = 1024
TRUNCATION_MARKER = b"\n[support-bundle: truncated]\n"


@dataclass
class AddResult:
    arcname: str
    written_bytes: int
    truncated: bool
    skipped: bool = False


@dataclass
class BundleArchive:
    output_path: Path
    per_file_cap: int = DEFAULT_PER_FILE_CAP
    total_cap: int = DEFAULT_TOTAL_CAP
    metadata_reserve: int = DEFAULT_METADATA_RESERVE
    archive_overhead_reserve: int = DEFAULT_ARCHIVE_OVERHEAD_RESERVE
    max_entries: int = DEFAULT_MAX_ENTRIES
    added: list[AddResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    bytes_written: int = 0
    _tar: tarfile.TarFile | None = None
    _gzip: gzip.GzipFile | None = None
    _raw: BinaryIO | None = None
    _checksum_lines: list[str] = field(default_factory=list)
    _arcnames: set[str] = field(default_factory=set)
    _temp_path: Path | None = None

    def __enter__(self) -> "BundleArchive":
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        # Construct the gzip layer explicitly so the header timestamp and
        # filename are stable. Tar entries also use fixed ownership/mtime in
        # ``_write`` below. Given the same ordered inputs this produces the
        # same archive bytes, which makes checksums useful during support-side
        # ingestion and regression tests.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.output_path.name}.", suffix=".tmp", dir=str(self.output_path.parent),
        )
        self._temp_path = Path(tmp_name)
        os.chmod(tmp_name, 0o640)
        self._raw = os.fdopen(fd, "wb")
        self._gzip = gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=6,
            fileobj=self._raw,
            mtime=0,
        )
        self._tar = tarfile.open(fileobj=self._gzip, mode="w", format=tarfile.GNU_FORMAT)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        close_error: BaseException | None = None
        try:
            if self._tar is not None:
                self._tar.close()
        except BaseException as close_exc:  # ensure the temp file is removed
            close_error = close_exc
        finally:
            self._tar = None
            try:
                if self._gzip is not None:
                    self._gzip.close()
            except BaseException as close_exc:
                close_error = close_error or close_exc
            finally:
                self._gzip = None
                if self._raw is not None:
                    try:
                        self._raw.flush()
                        os.fsync(self._raw.fileno())
                    except BaseException as close_exc:
                        close_error = close_error or close_exc
                    finally:
                        self._raw.close()
                    self._raw = None

        temp_path = self._temp_path
        self._temp_path = None
        if temp_path is not None:
            if exc_type is None and close_error is None:
                os.replace(temp_path, self.output_path)
                _fsync_directory(self.output_path.parent)
            else:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
        if exc_type is None and close_error is not None:
            raise close_error

    def add(self, arcname: str, data: bytes | str, *, required: bool = False) -> AddResult:
        """Add a single file to the tarball under ``arcname``.

        Truncates if larger than per-file cap. Optional collector files are
        stopped before the metadata reserve, while ``required`` top-level
        metadata may consume that reserve. Unsafe or duplicate member names
        are rejected instead of creating an ambiguous/path-traversing archive.
        """
        if self._tar is None:
            raise RuntimeError("BundleArchive must be used as a context manager")
        _validate_arcname(arcname)
        if arcname in self._arcnames:
            raise ValueError(f"duplicate archive member: {arcname!r}")
        if len(self._arcnames) >= self.max_entries:
            raise ValueError(f"archive entry limit exceeded ({self.max_entries})")

        payload = data.encode("utf-8", errors="replace") if isinstance(data, str) else data
        truncated = False
        if len(payload) > self.per_file_cap:
            # The marker is part of the per-file cap, not extra bytes beyond
            # it. Keeping the contract exact prevents a collection with many
            # capped logs from unexpectedly exceeding the bundle limit.
            keep = max(0, self.per_file_cap - len(TRUNCATION_MARKER))
            payload = payload[:keep] + TRUNCATION_MARKER[: self.per_file_cap - keep]
            truncated = True

        limit = self._required_payload_limit() if required else self._optional_payload_limit()
        if self.bytes_written + len(payload) > limit:
            self.skipped.append(arcname)
            return AddResult(
                arcname=arcname,
                written_bytes=0,
                truncated=truncated,
                skipped=True,
            )

        result = self._write(arcname, payload, truncated=truncated)
        self._checksum_lines.append(f"{hashlib.sha256(payload).hexdigest()}  {arcname}")
        return result

    def _write(self, arcname: str, payload: bytes, *, truncated: bool) -> AddResult:
        assert self._tar is not None
        info = tarfile.TarInfo(name=arcname)
        info.size = len(payload)
        info.mtime = 0
        info.mode = 0o640
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        self._tar.addfile(info, io.BytesIO(payload))

        self.bytes_written += len(payload)
        result = AddResult(arcname=arcname, written_bytes=len(payload), truncated=truncated)
        self.added.append(result)
        self._arcnames.add(arcname)
        return result

    def finalize_checksums(self, arcname: str = "checksums.sha256") -> AddResult:
        """Append a SHA256SUMS-style file listing every entry already added."""
        body = ("\n".join(self._checksum_lines) + "\n").encode("utf-8")
        # Append directly to avoid the checksum file checksumming itself.
        if self._tar is None:
            raise RuntimeError("BundleArchive must be used as a context manager")
        _validate_arcname(arcname)
        if arcname in self._arcnames:
            raise ValueError(f"duplicate archive member: {arcname!r}")
        if self.bytes_written + len(body) > self._required_payload_limit():
            raise ValueError("checksum manifest does not fit reserved archive capacity")
        return self._write(arcname, body, truncated=False)

    def _effective_reserve(self, configured: int, divisor: int) -> int:
        """Scale reserves down for deliberately tiny caps used by tests."""
        return min(max(0, configured), max(0, self.total_cap // divisor))

    def _overhead_reserve(self) -> int:
        return self._effective_reserve(self.archive_overhead_reserve, 50)

    def _metadata_reserve(self) -> int:
        return self._effective_reserve(self.metadata_reserve, 100)

    def _required_payload_limit(self) -> int:
        return max(0, self.total_cap - self._overhead_reserve())

    def _optional_payload_limit(self) -> int:
        return max(0, self._required_payload_limit() - self._metadata_reserve())


def _validate_arcname(arcname: str) -> None:
    """Require a canonical, relative POSIX member name.

    Archives are normally inspected before extraction, but rejecting absolute,
    parent-traversing, Windows-style and control-character names makes them safe
    for conventional tooling too.
    """
    if not arcname or "\\" in arcname or "\x00" in arcname:
        raise ValueError(f"unsafe archive member name: {arcname!r}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in arcname):
        raise ValueError(f"unsafe archive member name: {arcname!r}")
    path = PurePosixPath(arcname)
    if path.is_absolute() or arcname != path.as_posix():
        raise ValueError(f"unsafe archive member name: {arcname!r}")
    if any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe archive member name: {arcname!r}")
    if path.parts and ":" in path.parts[0]:
        raise ValueError(f"unsafe archive member name: {arcname!r}")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
