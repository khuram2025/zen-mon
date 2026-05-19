"""Daily retention sweep for support bundles.

Keeps at most ``MAX_KEEP`` bundles or anything newer than ``MAX_AGE_DAYS``,
whichever is smaller. Run as root by ``zenplus-support-cleanup.timer``.
"""

from __future__ import annotations

import logging
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
    bundles = sorted(js.BUNDLES_DIR.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)

    # Keep the newest MAX_KEEP regardless of age.
    keep = set(bundles[:MAX_KEEP])
    removed = 0
    for path in bundles:
        if path in keep:
            continue
        if path.stat().st_mtime < cutoff or path not in keep:
            _delete_bundle_and_state(path)
            removed += 1

    # Sweep orphaned job/request files (no corresponding bundle and >24h old).
    _sweep_dir(js.JOBS_DIR, "*.json", cutoff_override=time.time() - 86400)
    _sweep_dir(js.REQUESTS_DIR, "*.json", cutoff_override=time.time() - 86400)

    logger.info("retention sweep complete: removed=%d kept=%d", removed, len(keep))
    return 0


def _delete_bundle_and_state(bundle: Path) -> None:
    job_id = bundle.stem.removesuffix(".tar")
    if not js.is_valid_job_id(job_id):
        # Don't delete things whose name doesn't look like ours.
        return
    try:
        bundle.unlink()
    except OSError:
        pass
    for path in (js.job_path(job_id), js.request_path(job_id)):
        try:
            path.unlink()
        except (OSError, ValueError):
            pass


def _sweep_dir(directory: Path, glob: str, *, cutoff_override: float) -> None:
    for path in directory.glob(glob):
        try:
            if path.stat().st_mtime < cutoff_override:
                path.unlink()
        except OSError:
            continue


if __name__ == "__main__":
    sys.exit(main())
