"""Secure HTTPS downloader with resume support and integrity verification."""

import logging
import tempfile
from pathlib import Path

import httpx

from .crypto import sha256_file

logger = logging.getLogger("zenplus.updater")

CHUNK_SIZE = 65536  # 64 KB


class DownloadError(Exception):
    """Raised when a download fails."""


def download_package(
    url: str,
    dest_path: str,
    expected_sha256: str,
    headers: dict | None = None,
    timeout: int = 600,
    verify_tls: bool = True,
) -> str:
    """Download an update package with integrity verification.

    Supports resume on partial downloads.
    Returns the path to the verified file.
    """
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Check for partial download to resume
    resume_from = 0
    if dest.exists():
        existing_hash = sha256_file(str(dest))
        if existing_hash == expected_sha256:
            logger.info("Package already downloaded and verified: %s", dest)
            return str(dest)
        resume_from = dest.stat().st_size
        logger.info("Resuming download from byte %d", resume_from)

    req_headers = dict(headers or {})
    if resume_from > 0:
        req_headers["Range"] = f"bytes={resume_from}-"

    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout, connect=30),
            verify=verify_tls,
            follow_redirects=True,
        ) as client:
            with client.stream("GET", url, headers=req_headers) as response:
                if response.status_code == 416:
                    # Range not satisfiable — start fresh
                    logger.warning("Resume not supported, starting fresh download")
                    resume_from = 0
                    dest.unlink(missing_ok=True)
                    return download_package(
                        url, dest_path, expected_sha256, headers, timeout, verify_tls
                    )

                response.raise_for_status()

                total_size = response.headers.get("content-length")
                if total_size:
                    total_size = int(total_size) + resume_from

                mode = "ab" if resume_from > 0 else "wb"
                downloaded = resume_from

                with open(dest, mode) as f:
                    for chunk in response.iter_bytes(chunk_size=CHUNK_SIZE):
                        f.write(chunk)
                        downloaded += len(chunk)

                        if total_size and total_size > 0:
                            pct = (downloaded / total_size) * 100
                            if downloaded % (CHUNK_SIZE * 16) == 0:
                                logger.debug(
                                    "Download progress: %.1f%% (%d / %d bytes)",
                                    pct,
                                    downloaded,
                                    total_size,
                                )

    except httpx.HTTPStatusError as e:
        raise DownloadError(f"HTTP error {e.response.status_code}: {e}") from e
    except httpx.RequestError as e:
        raise DownloadError(f"Download failed: {e}") from e

    # Verify integrity
    actual_hash = sha256_file(str(dest))
    if actual_hash != expected_sha256:
        dest.unlink(missing_ok=True)
        raise DownloadError(
            f"Integrity check failed: expected {expected_sha256}, got {actual_hash}"
        )

    logger.info("Download complete and verified: %s (%d bytes)", dest, downloaded)
    return str(dest)
