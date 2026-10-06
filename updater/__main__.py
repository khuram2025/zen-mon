"""Allow running: python -m updater"""

import os
from pathlib import Path

from .crypto import SecurityError
from .trust import require_root_owned

# Python selects config/__init__.py ahead of config.py. Validate the data
# directory BEFORE importing agent (which imports .config). No planted module
# is ever imported, even when an older installation has unsafe permissions.
config_dir = Path(__file__).resolve().parent / "config"
if os.path.lexists(config_dir):
    if any(entry.name.startswith("__init__.") for entry in config_dir.iterdir()):
        raise SecurityError("Updater config directory contains an unexpected Python package")
    if os.geteuid() == 0:
        require_root_owned(config_dir, what="updater enrollment directory")

from .agent import cli

if __name__ == "__main__":
    cli()
