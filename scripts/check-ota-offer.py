"""Read-only appliance-side check for the currently offered OTA release."""

from __future__ import annotations

import argparse
import json

from updater.agent import _api_headers, check_for_update
from updater.config import load_config
from updater.downloader import download_package


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--download-to",
        help="optionally download and hash-verify the offered package without applying it",
    )
    args = parser.parse_args()

    config = load_config()
    release = check_for_update(config)
    if not release:
        raise RuntimeError("No OTA release is currently offered to this appliance")
    if args.download_to:
        download_package(
            url=release["package_url"],
            dest_path=args.download_to,
            expected_sha256=release["package_sha256"],
            headers=_api_headers(config),
            timeout=config.server.download_timeout_seconds,
            verify_tls=config.security.verify_tls,
        )
    print(
        json.dumps(
            {
                key: release.get(key)
                for key in ("id", "version", "severity", "package_sha256", "size")
            }
            | {"download_verified": bool(args.download_to)},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
