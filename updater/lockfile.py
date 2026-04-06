"""File-based lock to prevent concurrent update runs."""

import logging
import os
import signal
from pathlib import Path

logger = logging.getLogger("zenplus.updater")

DEFAULT_LOCK_PATH = "/opt/zenplus/updater/updater.lock"


class LockError(Exception):
    """Raised when the lock cannot be acquired."""


class UpdateLock:
    """File-based lock with PID staleness detection."""

    def __init__(self, path: str = DEFAULT_LOCK_PATH):
        self.path = Path(path)
        self._fd = None

    def acquire(self) -> None:
        """Acquire the lock. Raises LockError if already held."""
        if self.path.exists():
            try:
                old_pid = int(self.path.read_text().strip())
                # Check if process is still alive
                os.kill(old_pid, 0)
                raise LockError(
                    f"Update already in progress (PID {old_pid})"
                )
            except (ValueError, ProcessLookupError, PermissionError):
                # Stale lock file — previous process died
                logger.warning("Removing stale lock file (PID gone)")
                self.path.unlink(missing_ok=True)
            except LockError:
                raise

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()))
        logger.debug("Lock acquired: PID %d", os.getpid())

    def release(self) -> None:
        """Release the lock."""
        try:
            if self.path.exists():
                pid = int(self.path.read_text().strip())
                if pid == os.getpid():
                    self.path.unlink(missing_ok=True)
                    logger.debug("Lock released")
        except (ValueError, OSError) as e:
            logger.warning("Error releasing lock: %s", e)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()
