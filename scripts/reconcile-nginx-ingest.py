#!/usr/bin/env python3
"""Insert or refresh the exact host-results location in a ZenPlus nginx site."""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from pathlib import Path

BEGIN = "# BEGIN ZenPlus host-results ingest timeout"
END = "# END ZenPlus host-results ingest timeout"


def _block(indent: str) -> str:
    lines = [
        BEGIN,
        "# Host inventory uploads must not inherit the long-lived /api/ timeout.",
        "location = /api/v1/agents/results/host {",
        "    proxy_pass http://127.0.0.1:8000;",
        "    proxy_set_header Host $host;",
        "    proxy_set_header X-Real-IP $remote_addr;",
        "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "    proxy_set_header X-Forwarded-Proto $scheme;",
        "    proxy_http_version 1.1;",
        "    proxy_buffering off;",
        "    proxy_cache off;",
        "    proxy_read_timeout 45s;",
        "    proxy_send_timeout 45s;",
        "}",
        END,
    ]
    return "\n".join(indent + line for line in lines)


def reconcile(path: Path) -> bool:
    original = path.read_text(encoding="utf-8")
    managed = re.compile(
        rf"(?ms)^(?P<indent>[ \t]*){re.escape(BEGIN)}$.*?"
        rf"^(?P=indent){re.escape(END)}$\n?"
    )
    updated, managed_count = managed.subn(
        lambda match: _block(match.group("indent")) + "\n", original
    )

    if managed_count == 0:
        api_location = re.compile(
            r"(?m)^(?P<indent>[ \t]*)location\s+/api/\s*\{"
        )
        updated, api_count = api_location.subn(
            lambda match: _block(match.group("indent")) + "\n\n" + match.group(0),
            original,
        )
        if api_count == 0:
            raise RuntimeError("generic ZenPlus location /api/ was not found")

    if updated == original:
        return False

    metadata = path.stat()
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, stat.S_IMODE(metadata.st_mode))
        if hasattr(os, "chown"):
            os.chown(temp_name, metadata.st_uid, metadata.st_gid)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return True


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} /path/to/zenplus-nginx.conf", file=sys.stderr)
        return 2
    target = Path(sys.argv[1])
    if not target.is_file():
        print(f"nginx configuration not found: {target}", file=sys.stderr)
        return 1
    try:
        changed = reconcile(target)
    except (OSError, UnicodeError, RuntimeError) as exc:
        print(f"failed to reconcile {target}: {exc}", file=sys.stderr)
        return 1
    print("changed" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
