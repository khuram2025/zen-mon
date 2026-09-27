"""SHA-256 of published agent packages, cached by file identity.

Agent packages are immutable once published and the controller re-verifies
them on every package listing, manifest request and download. Hashing the
~230 MB installers each time (and every retained version on each listing)
ties up a worker for seconds and re-reads gigabytes from disk.

The cache key is the file's full identity: device, inode, size, and the
modification and inode-change times in nanoseconds. Any write, truncate,
rename-over or metadata change updates ctime, which user space cannot set
back, so a tampered file always misses the cache and is hashed again. The
drift quarantine in sync_agent_packages and the pre-download integrity check
therefore keep their guarantees.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

_CHUNK = 1024 * 1024
_lock = threading.Lock()
_cache: dict[str, tuple[tuple[int, int, int, int, int], str]] = {}


def _identity(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 of ``path``, reusing a digest for an unchanged file."""
    path = Path(path)
    key = str(path.resolve())
    before = _identity(path.stat())
    with _lock:
        cached = _cache.get(key)
    if cached is not None and cached[0] == before:
        return cached[1]

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    # Only cache a digest if the file did not change while it was read.
    if _identity(path.stat()) == before:
        with _lock:
            _cache[key] = (before, value)
    return value


def clear_cache() -> None:
    with _lock:
        _cache.clear()
