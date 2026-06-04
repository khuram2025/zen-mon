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
import io
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_PER_FILE_CAP = 5 * 1024 * 1024
DEFAULT_TOTAL_CAP = 50 * 1024 * 1024
TRUNCATION_MARKER = b"\n[support-bundle: truncated]\n"


@dataclass
class AddResult:
    arcname: str
    written_bytes: int
    truncated: bool


@dataclass
class BundleArchive:
    output_path: Path
    per_file_cap: int = DEFAULT_PER_FILE_CAP
    total_cap: int = DEFAULT_TOTAL_CAP
    added: list[AddResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    bytes_written: int = 0
    _tar: tarfile.TarFile | None = None
    _checksum_lines: list[str] = field(default_factory=list)

    def __enter__(self) -> "BundleArchive":
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._tar = tarfile.open(self.output_path, "w:gz", compresslevel=6)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._tar is not None:
            self._tar.close()
            self._tar = None

    def add(self, arcname: str, data: bytes | str) -> AddResult:
        """Add a single file to the tarball under ``arcname``.

        Truncates if larger than per-file cap. Skips if total cap is exceeded.
        """
        if self._tar is None:
            raise RuntimeError("BundleArchive must be used as a context manager")

        payload = data.encode("utf-8", errors="replace") if isinstance(data, str) else data
        truncated = False
        if len(payload) > self.per_file_cap:
            payload = payload[: self.per_file_cap] + TRUNCATION_MARKER
            truncated = True

        # Skip if it would push us over the total cap.
        if self.bytes_written + len(payload) > self.total_cap:
            self.skipped.append(arcname)
            return AddResult(arcname=arcname, written_bytes=0, truncated=truncated)

        info = tarfile.TarInfo(name=arcname)
        info.size = len(payload)
        info.mtime = int(time.time())
        info.mode = 0o640
        self._tar.addfile(info, io.BytesIO(payload))

        self.bytes_written += len(payload)
        digest = hashlib.sha256(payload).hexdigest()
        self._checksum_lines.append(f"{digest}  {arcname}")
        result = AddResult(arcname=arcname, written_bytes=len(payload), truncated=truncated)
        self.added.append(result)
        return result

    def finalize_checksums(self, arcname: str = "checksums.sha256") -> None:
        """Append a SHA256SUMS-style file listing every entry already added."""
        body = ("\n".join(self._checksum_lines) + "\n").encode("utf-8")
        # Append directly to avoid the checksum file checksumming itself.
        if self._tar is None:
            raise RuntimeError("BundleArchive must be used as a context manager")
        info = tarfile.TarInfo(name=arcname)
        info.size = len(body)
        info.mtime = int(time.time())
        info.mode = 0o640
        self._tar.addfile(info, io.BytesIO(body))
        self.bytes_written += len(body)
