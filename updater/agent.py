"""ZenPlus Update Agent — main daemon logic.

Lifecycle:
1. Load config, acquire lock
2. Collect inventory, check in with server
3. If update available: download, verify, extract, apply, report
4. Release lock
"""

import json
import logging
import logging.handlers
import os
import shutil
import sys
import tarfile
import tempfile
from base64 import b64decode, b64encode
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import __version__
from .config import AgentConfig, load_config, save_config, save_subscription
from .crypto import (
    SecurityError,
    sha256_file,
    verify_checksums,
    verify_manifest,
    verify_signature,
    load_public_key,
)
from .downloader import DownloadError, download_package
from .health import HealthCheckError, check_http
from . import history
from .inventory import collect_inventory, get_current_version
from .lockfile import LockError, UpdateLock

logger = logging.getLogger("zenplus.updater")

ZENPLUS_DIR = Path("/opt/zenplus")
TEMP_DIR = Path("/tmp/zenplus-updates")


def setup_logging(cfg: AgentConfig) -> None:
    """Configure rotating file + console logging."""
    log_path = Path(cfg.logging.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("zenplus.updater")
    root.setLevel(getattr(logging, cfg.logging.log_level.upper(), logging.INFO))

    # Rotating file handler
    fh = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=cfg.logging.max_log_size_mb * 1024 * 1024,
        backupCount=cfg.logging.log_rotate_count,
    )
    fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    root.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root.addHandler(ch)


def _api_headers(cfg: AgentConfig) -> dict:
    """Build API request headers.

    Auth headers are only included when credentials exist. The registration
    endpoint runs *before* an api_key is issued, and httpx rejects an
    "Authorization: Bearer " header (empty value) as malformed before the
    request is even sent.
    """
    headers = {
        "User-Agent": f"zenplus-updater/{__version__}",
        "Content-Type": "application/json",
    }
    if cfg.appliance.api_key:
        headers["Authorization"] = f"Bearer {cfg.appliance.api_key}"
    if cfg.appliance.id:
        headers["X-Appliance-ID"] = cfg.appliance.id
    return headers


def _api_client(cfg: AgentConfig) -> httpx.Client:
    """Create an HTTP client for the update server."""
    return httpx.Client(
        base_url=cfg.server.url,
        headers=_api_headers(cfg),
        timeout=httpx.Timeout(30, connect=10),
        verify=cfg.security.verify_tls,
        follow_redirects=True,
    )


def register(cfg: AgentConfig, registration_token: str = "") -> AgentConfig:
    """Register this appliance with the central server.

    Called once during initial setup. Saves appliance_id and api_key.

    Args:
        cfg: Agent configuration.
        registration_token: License key from the Zentryc subscription page.
    """
    if not registration_token:
        raise ValueError("registration_token (license key) is required")

    inventory = collect_inventory()

    with _api_client(cfg) as client:
        resp = client.post(
            "/api/v1/appliances/register",
            json={
                "hostname": inventory["hostname"],
                "arch": inventory["arch"],
                "os_version": inventory["os_version"],
                "current_version": inventory["current_version"],
                "registration_token": registration_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    cfg.appliance.id = data["appliance_id"]
    cfg.appliance.api_key = data["api_key"]
    save_config(cfg)

    # Persist subscription data from registration response
    if data.get("subscription"):
        save_subscription(data["subscription"])
        logger.info(
            "Subscription: %s (plan=%s, slots=%s/%s)",
            data["subscription"].get("name", ""),
            data["subscription"].get("plan", ""),
            data["subscription"].get("used_slots", "?"),
            data["subscription"].get("max_appliances", "?"),
        )

    logger.info("Registered appliance: id=%s", cfg.appliance.id)
    return cfg


def checkin(cfg: AgentConfig) -> dict | None:
    """Check in with the server and get update instructions.

    Returns release info dict if an update is available, None otherwise.
    """
    inventory = collect_inventory()

    try:
        with _api_client(cfg) as client:
            resp = client.post("/api/v1/appliances/checkin", json=inventory)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.error("Check-in failed: %s", e)
        return None

    # Persist subscription data from check-in response
    if data.get("subscription"):
        save_subscription(data["subscription"])
        sub = data["subscription"]
        if not sub.get("is_active") or sub.get("is_expired"):
            logger.warning(
                "Subscription issue: active=%s, expired=%s, expires_at=%s",
                sub.get("is_active"), sub.get("is_expired"),
                sub.get("expires_at", ""),
            )

    if data.get("next_action") == "update" and data.get("release"):
        release = data["release"]
        logger.info(
            "Update available: %s → %s (severity: %s)",
            get_current_version(),
            release["version"],
            release.get("severity", "normal"),
        )
        return release

    logger.info("No updates available")
    return None


def query_subscription(cfg: AgentConfig) -> dict | None:
    """Query subscription info from the OTA server on demand.

    Returns subscription dict or None on failure.
    """
    try:
        with _api_client(cfg) as client:
            resp = client.get("/api/v1/appliances/subscription")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.error("Subscription query failed: %s", e)
        return None

    sub = data.get("subscription")
    if sub:
        save_subscription(sub)
    return sub


def check_for_update(cfg: AgentConfig) -> dict | None:
    """Explicitly check for updates without full check-in."""
    try:
        with _api_client(cfg) as client:
            resp = client.get(
                "/api/v1/updates/check",
                params={
                    "current_version": get_current_version(),
                    "arch": collect_inventory()["arch"],
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.error("Update check failed: %s", e)
        return None

    if data.get("available") and data.get("release"):
        return data["release"]
    return None


def report_status(
    cfg: AgentConfig,
    release_id: str,
    status: str,
    from_version: str,
    to_version: str,
    error_message: str = "",
    log_data: str = "",
    changelog: str = "",
    severity: str = "normal",
) -> None:
    """Report update status to both the local history file and the server.

    The local history file powers the dashboard Updates tab. We write it
    first (best effort) so the UI still shows state even if the server
    report fails.
    """
    # 1. Local history — drives the Updates tab banners and history list
    try:
        history.add_record(
            version=to_version,
            from_version=from_version,
            status=status,
            error=error_message,
            changelog=changelog,
            severity=severity,
        )
    except Exception as e:
        logger.warning("Could not persist local history: %s", e)

    # 2. Remote OTA server
    try:
        with _api_client(cfg) as client:
            resp = client.post(
                "/api/v1/updates/report",
                json={
                    "release_id": release_id,
                    "status": status,
                    "from_version": from_version,
                    "to_version": to_version,
                    "error_message": error_message,
                    "log_data": log_data,
                },
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.error("Failed to report status: %s", e)


def download_and_extract(
    cfg: AgentConfig, release: dict
) -> tuple[str, dict]:
    """Download, verify, and extract an update package.

    Returns (extract_dir, manifest_dict).
    """
    version = release["version"]
    package_url = release["package_url"]
    package_sha256 = release["package_sha256"]
    manifest_sig_b64 = release.get("manifest_sig", "")

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    package_path = str(TEMP_DIR / f"update-{version}.zup")
    extract_dir = str(TEMP_DIR / f"update-{version}")

    # Download
    logger.info("Downloading update package v%s ...", version)
    download_package(
        url=package_url,
        dest_path=package_path,
        expected_sha256=package_sha256,
        headers=_api_headers(cfg),
        timeout=cfg.server.download_timeout_seconds,
        verify_tls=cfg.security.verify_tls,
    )

    # Extract
    logger.info("Extracting package ...")
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir)
    os.makedirs(extract_dir)

    with tarfile.open(package_path, "r:gz") as tar:
        # Security: prevent path traversal
        for member in tar.getmembers():
            if member.name.startswith("/") or ".." in member.name:
                raise SecurityError(
                    f"Dangerous path in archive: {member.name}"
                )
        tar.extractall(extract_dir)

    # Verify manifest signature
    manifest_path = os.path.join(extract_dir, "manifest.json")
    sig_path = os.path.join(extract_dir, "manifest.json.sig")

    if not os.path.exists(manifest_path):
        raise SecurityError("Package missing manifest.json")

    if not os.path.exists(sig_path) and manifest_sig_b64:
        # Signature provided by server API instead of in package
        Path(sig_path).write_bytes(b64decode(manifest_sig_b64))

    manifest = verify_manifest(
        manifest_path,
        sig_path,
        cfg.security.public_key_path,
        max_age_days=cfg.security.max_manifest_age_days,
    )

    # Verify file checksums
    checksums_path = os.path.join(extract_dir, "checksums.sha256")
    if os.path.exists(checksums_path):
        failures = verify_checksums(checksums_path, extract_dir)
        if failures:
            raise SecurityError(
                f"Checksum verification failed: {', '.join(failures)}"
            )
        logger.info("All file checksums verified")

    return extract_dir, manifest


def run_update(cfg: AgentConfig, release: dict) -> bool:
    """Execute the full update lifecycle.

    Returns True on success, False on failure (with rollback).
    """
    from .executor import execute_manifest, rollback_manifest, ExecutionError

    version = release["version"]
    release_id = release.get("id", release.get("update_id", ""))
    from_version = get_current_version()
    changelog = release.get("changelog", "")
    severity = release.get("severity", "normal")

    logger.info("=" * 60)
    logger.info("Starting update: %s → %s", from_version, version)
    logger.info("=" * 60)

    # Report: downloading
    report_status(
        cfg, release_id, "downloading", from_version, version,
        changelog=changelog, severity=severity,
    )

    try:
        extract_dir, manifest = download_and_extract(cfg, release)
    except (DownloadError, SecurityError) as e:
        logger.error("Download/verification failed: %s", e)
        report_status(
            cfg, release_id, "failed", from_version, version,
            error_message=str(e), changelog=changelog, severity=severity,
        )
        return False

    # Report: applying
    report_status(
        cfg, release_id, "applying", from_version, version,
        changelog=changelog, severity=severity,
    )

    try:
        execute_manifest(manifest, extract_dir, cfg)
    except ExecutionError as e:
        logger.error("Update failed: %s", e)
        report_status(
            cfg, release_id, "failed", from_version, version,
            error_message=str(e), changelog=changelog, severity=severity,
        )
        return False

    # Schema gate. Every migrate-*.sql on disk has just been refreshed by
    # apply_code; converge both databases with it and refuse to stamp the new
    # version unless the result is clean. A passing HTTP health check is not
    # evidence that the schema matches the code — an appliance once ran a whole
    # release with its ClickHouse SNMP tables missing and reported healthy.
    try:
        from .schema_gate import sync_and_verify
        schema_status = sync_and_verify()
    except Exception as e:
        logger.exception("Schema gate raised: %s", e)
        schema_status = {"ok": False, "problems": [f"schema gate error: {e}"]}

    if not schema_status.get("ok"):
        problems = schema_status.get("problems", [])
        detail = "; ".join(problems[:10]) or "unknown schema drift"
        logger.error(
            "Schema does not match the installed code after update — rolling back. %s",
            detail,
        )
        rollback_manifest(manifest, extract_dir, cfg)
        report_status(
            cfg, release_id, "failed", from_version, version,
            error_message=f"Schema verification failed: {detail}",
            changelog=changelog, severity=severity,
        )
        return False

    # Update version file — only now, with code and schema proven consistent.
    version_file = ZENPLUS_DIR / ".version"
    version_file.write_text(
        f"{version}\n{datetime.now(timezone.utc).isoformat()}\n"
    )

    # Report: success
    report_status(
        cfg, release_id, "success", from_version, version,
        changelog=changelog, severity=severity,
    )
    logger.info("Update completed successfully: %s → %s", from_version, version)

    # Cleanup
    try:
        shutil.rmtree(str(TEMP_DIR / f"update-{version}"), ignore_errors=True)
        Path(str(TEMP_DIR / f"update-{version}.zup")).unlink(missing_ok=True)
    except OSError:
        pass

    return True


def main(config_path: str | None = None, check_only: bool = False) -> int:
    """Main agent entry point.

    Args:
        config_path: Override path to agent.conf
        check_only: If True, only check for updates, don't apply

    Returns:
        0 on success/no-update, 1 on failure
    """
    cfg = load_config(config_path)
    setup_logging(cfg)

    logger.info("ZenPlus Update Agent v%s starting", __version__)

    if not cfg.appliance.id or not cfg.appliance.api_key:
        logger.error(
            "Appliance not registered. Run 'zenplus-updater --register' first."
        )
        return 1

    lock = UpdateLock()
    try:
        lock.acquire()
    except LockError as e:
        logger.warning("Cannot acquire lock: %s", e)
        return 1

    try:
        # Check in and look for updates
        release = checkin(cfg)

        if not release:
            return 0

        if check_only:
            print(
                f"Update available: {get_current_version()} → {release['version']}"
            )
            print(f"  Changelog: {release.get('changelog', 'N/A')}")
            print(f"  Severity: {release.get('severity', 'normal')}")
            return 0

        # Apply update
        success = run_update(cfg, release)
        return 0 if success else 1

    except Exception as e:
        logger.exception("Unexpected error: %s", e)
        return 1
    finally:
        lock.release()


def cli() -> None:
    """CLI entry point with argument parsing."""
    import argparse

    parser = argparse.ArgumentParser(
        description="ZenPlus OTA Update Agent",
        prog="zenplus-updater",
    )
    parser.add_argument(
        "--config", "-c",
        help="Path to agent.conf",
        default=None,
    )
    parser.add_argument(
        "--register",
        metavar="LICENSE_KEY",
        help="Register this appliance with the central server using the given license key",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check for updates without applying",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"zenplus-updater {__version__}",
    )

    args = parser.parse_args()

    if args.register:
        cfg = load_config(args.config)
        setup_logging(cfg)
        try:
            register(cfg, registration_token=args.register)
            print(f"Registered successfully. Appliance ID: {cfg.appliance.id}")
        except Exception as e:
            print(f"Registration failed: {e}", file=sys.stderr)
            sys.exit(1)
        return

    sys.exit(main(config_path=args.config, check_only=args.check))
