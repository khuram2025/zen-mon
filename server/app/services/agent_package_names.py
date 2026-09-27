"""Agent package file names in the artifact store.

Two package flavours are published per platform and version:

- ``full``: server monitoring and APM, named ``zenplus-agent-<version>.<ext>``.
  This is the historical name, so agents released before the split (which
  all carry the APM bundle) keep upgrading to a package that still has it.
- ``core``: server monitoring only, named ``zenplus-agent-core-<version>.<ext>``.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional

FLAVOR_CORE = "core"
FLAVOR_FULL = "full"
FLAVORS = (FLAVOR_CORE, FLAVOR_FULL)
FLAVOR_PATTERN = "^(core|full)$"

_PKG_NAME_RE = re.compile(
    r"^zenplus-agent(?P<core>-core)?[-_]"
    r"(?P<version>\d+\.\d+\.\d+(?:[-.][A-Za-z0-9]+)?)\."
    r"(?P<ext>msi|exe|pkg|tar\.gz)$"
)


class PackageName(NamedTuple):
    flavor: str
    version: str
    ext: str


def parse_package_name(name: str) -> Optional[PackageName]:
    """Return flavour, version and extension, or None for an unrelated file."""
    match = _PKG_NAME_RE.match(name)
    if not match:
        return None
    flavor = FLAVOR_CORE if match.group("core") else FLAVOR_FULL
    return PackageName(flavor, match.group("version"), match.group("ext"))


def version_key(version: str) -> list[int]:
    return [int(p) if p.isdigit() else 0 for p in version.split("-")[0].split(".")]


def download_path(platform: str, flavor: str) -> str:
    """Stable download URL for the latest package of one flavour."""
    path = f"/api/v1/agents/packages/{platform}/latest"
    return path if flavor == FLAVOR_FULL else f"{path}?flavor={flavor}"


def versioned_download_path(platform: str, flavor: str, version: str) -> str:
    """Download URL for one published version, used for pinned upgrades."""
    path = f"/api/v1/agents/packages/{platform}/versions/{version}"
    return path if flavor == FLAVOR_FULL else f"{path}?flavor={flavor}"
