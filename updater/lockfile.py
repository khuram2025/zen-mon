"""File-based lock to prevent concurrent update runs."""

import logging
import fcntl
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Compatibility with a still-running updater from before flock.
            previous = os.read(fd, 32).decode().strip()
            if previous.isdigit() and int(previous) != os.getpid():
                try:
                    os.kill(int(previous), 0)
                except ProcessLookupError:
                    pass
                else:
                    raise LockError('Update already in progress')
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, str(os.getpid()).encode())
            self._fd = fd
        except (BlockingIOError, LockError):
            os.close(fd)
            raise LockError('Update already in progress') from None
        except BaseException:
            os.close(fd)
            raise
        logger.debug("Lock acquired: PID %d", os.getpid())

    def release(self) -> None:
        """Release the lock."""
        if self._fd is not None:
            os.ftruncate(self._fd, 0)
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
            logger.debug('Lock released')

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()
