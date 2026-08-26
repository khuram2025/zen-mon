#!/usr/bin/env python3
"""Reconcile a just-applied OTA payload when the running updater is older.

The updater imports its step handlers before ``apply_code`` replaces them, so
the first release containing exact-tree support still executes the old overlay
handler.  ``run_hook`` already exists on old appliances; this small bridge loads
the newly installed reconciler after the overlay and removes stale code during
that same update.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    code_path = Path(__file__).resolve().parents[1]
    zenplus_dir = Path(os.environ.get("ZENPLUS_DIR", "/opt/zenplus")).resolve()
    sys.path.insert(0, str(zenplus_dir))

    from updater.code_inventory import reconcile_code_tree

    removed = reconcile_code_tree(code_path, zenplus_dir)
    print(f"Reconciled signed code payload; removed {removed} stale file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
