"""Daily retention sweep for support bundles.

Keeps at most ``MAX_KEEP`` bundles or anything newer than ``MAX_AGE_DAYS``,
whichever is smaller. Run as ``zenplus`` by
``zenplus-support-cleanup.timer``.
"""

from __future__ import annotations

import logging
import stat
import sys
import time
from pathlib import Path

from . import job_state as js


MAX_KEEP = 5
MAX_AGE_DAYS = 7

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s zenplus.support.cleanup: %(message)s")
logger = logging.getLogger("zenplus.support.cleanup")


def main() -> int:
    js.BUNDLES_DIR.mkdir(parents=True, exist_ok=True)
    js.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    js.REQUESTS_DIR.mkdir(parents=True, exist_ok=True)

    cutoff = time.time() - MAX_AGE_DAYS * 86400
    bundles = sorted(_owned_regular_files(js.BUNDLES_DIR, "*.tar.gz"), key=_mtime, reverse=True)

    # Apply both boundaries: a bundle is retained only when it is recent and
    # among the newest MAX_KEEP. The previous implementation kept the newest
    # five forever, contrary to the advertised seven-day privacy boundary.
    keep = {path for path in bundles if _mtime(path) >= cutoff}
    keep = set(sorted(keep, key=_mtime, reverse=True)[:MAX_KEEP])
    removed = 0
    for path in bundles:
        if path in keep:
            continue
        if _delete_bundle_and_state(path):
            removed += 1

    # Sweep stale state/request files only when there is no retained bundle.
    # Deleting the job state for a still-retained bundle makes it disappear
    # from the UI and prevents download even though the archive remains.
    orphan_cutoff = time.time() - 86400
    _sweep_dir(js.JOBS_DIR, "*.json", cutoff_override=orphan_cutoff, preserve_bundle=True)
    _sweep_dir(js.REQUESTS_DIR, "*.json", cutoff_override=orphan_cutoff, preserve_bundle=True)

    logger.info("retention sweep complete: removed=%d kept=%d", removed, len(keep))
    return 0


def _delete_bundle_and_state(bundle: Path) -> bool:
    job_id = bundle.stem.removesuffix(".tar")
    if not js.is_valid_job_id(job_id):
        # Don't delete things whose name doesn't look like ours.
        return False
    if not _is_regular_no_symlink(bundle):
        return False
    try:
        bundle.unlink()
    except OSError:
        return False
    for path in (js.job_path(job_id), js.request_path(job_id)):
        try:
            if _is_regular_no_symlink(path):
                path.unlink()
        except (OSError, ValueError):
            pass
    return True


def _sweep_dir(
    directory: Path,
    glob: str,
    *,
    cutoff_override: float,
    preserve_bundle: bool = False,
) -> None:
    for path in _owned_regular_files(directory, glob, json_names=True):
        try:
            if _mtime(path) >= cutoff_override:
                continue
            if preserve_bundle:
                job_id = path.stem
                bundle = js.bundle_path(job_id)
                if _is_regular_no_symlink(bundle):
                    continue
            if _is_regular_no_symlink(path):
                path.unlink()
        except (OSError, ValueError):
            continue


def _owned_regular_files(directory: Path, pattern: str, *, json_names: bool = False) -> list[Path]:
    out: list[Path] = []
    for path in directory.glob(pattern):
        job_id = path.stem if json_names else path.stem.removesuffix(".tar")
        if js.is_valid_job_id(job_id) and _is_regular_no_symlink(path):
            out.append(path)
    return out


def _is_regular_no_symlink(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _mtime(path: Path) -> float:
    return path.lstat().st_mtime


if __name__ == "__main__":
    sys.exit(main())
