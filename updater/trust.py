"""Root-owned trust settings for the OTA updater.

The updater runs as root, but ``updater/config/agent.conf`` is owned by the
unprivileged ``zenplus`` account because the API and the enrollment flow write
to it. Anything that decides *what the updater trusts* therefore cannot live
there: an attacker who gets code execution as ``zenplus`` could point the root
updater at their own server, turn TLS verification off, or swap the release
public key.

Trust settings come only from places the app account cannot write:

* ``/etc/zenplus/updater-trust.conf``  server URL, TLS verification, manifest
  age limit, legacy-package policy, revoked key fingerprints
* ``/opt/zenplus/updater/keys/zentryc-release.pub`` the default release key
* ``/etc/zenplus/updater-keys.d/*.pub`` additional release keys, so a key can be
  rotated without replacing the one that is already trusted

Every one of those files, and every directory above them, must be owned by
root and not writable by group or others, or the updater refuses to start.
"""

from __future__ import annotations

import configparser
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .crypto import SecurityError

logger = logging.getLogger("zenplus.updater")

TRUST_CONFIG_PATH = Path("/etc/zenplus/updater-trust.conf")
KEY_DROPIN_DIR = Path("/etc/zenplus/updater-keys.d")
DEFAULT_KEY_PATH = Path("/opt/zenplus/updater/keys/zentryc-release.pub")
DEFAULT_SERVER_URL = "https://zentryc.com"


@dataclass(frozen=True)
class TrustSettings:
    server_url: str = DEFAULT_SERVER_URL
    verify_tls: bool = True
    max_manifest_age_days: int = 30
    # Packages built before the signed file inventory existed. Off by default;
    # an administrator can turn it on in the root-owned file for an air-gapped
    # site that has to install an older signed release.
    allow_legacy_packages: bool = False
    public_key_paths: tuple[str, ...] = (str(DEFAULT_KEY_PATH),)
    revoked_key_fingerprints: frozenset[str] = frozenset()


def require_root_owned(
    path: Path,
    *,
    what: str,
    trusted_uid: int = 0,
    stop_at: Path = Path("/"),
) -> None:
    """Raise SecurityError unless ``path`` and every ancestor is tamper-proof.

    The path must not be a symlink, and it and each directory above it (up to
    and including ``stop_at``) must be owned by ``trusted_uid`` and not be
    writable by group or others.
    """
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise SecurityError(f"{what}: cannot stat {path}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise SecurityError(f"{what}: {path} is a symlink")

    real = Path(os.path.realpath(path))
    stop = Path(os.path.realpath(stop_at))
    chain = [real]
    if real != stop:
        for parent in real.parents:
            chain.append(parent)
            if parent == stop:
                break

    for node in chain:
        node_st = os.lstat(node)
        if node_st.st_uid != trusted_uid:
            raise SecurityError(
                f"{what}: {node} is owned by uid {node_st.st_uid}, expected {trusted_uid}"
            )
        if node_st.st_mode & 0o022:
            raise SecurityError(
                f"{what}: {node} is writable by group or others "
                f"(mode {stat.S_IMODE(node_st.st_mode):o})"
            )


def _parse_bool(value: str, name: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise SecurityError(f"{TRUST_CONFIG_PATH}: {name} must be true or false, got {value!r}")


def load_trust(
    path: Path = TRUST_CONFIG_PATH,
    *,
    key_dropin_dir: Path = KEY_DROPIN_DIR,
    default_key_path: Path = DEFAULT_KEY_PATH,
    trusted_uid: int = 0,
    stop_at: Path = Path("/"),
) -> TrustSettings:
    """Load trust settings, raising SecurityError if anything is not root-owned.

    A missing trust file is normal (compiled defaults apply); a file that exists
    but is writable by the app account is not, and stops the updater.
    """
    server_url = DEFAULT_SERVER_URL
    verify_tls = True
    max_age = 30
    allow_legacy = False
    revoked: set[str] = set()

    if os.path.lexists(path):
        require_root_owned(path, what="updater trust file", trusted_uid=trusted_uid, stop_at=stop_at)
        parser = configparser.ConfigParser()
        try:
            parser.read(path)
        except configparser.Error as exc:
            raise SecurityError(f"{path}: cannot parse: {exc}") from exc
        if parser.has_section("trust"):
            section = parser["trust"]
            server_url = section.get("server_url", server_url).strip()
            if "verify_tls" in section:
                verify_tls = _parse_bool(section["verify_tls"], "verify_tls")
            if "max_manifest_age_days" in section:
                try:
                    max_age = section.getint("max_manifest_age_days")
                except ValueError as exc:
                    raise SecurityError(f"{path}: max_manifest_age_days must be an integer") from exc
                if max_age < 1:
                    raise SecurityError(f"{path}: max_manifest_age_days must be at least 1")
            if "allow_legacy_packages" in section:
                allow_legacy = _parse_bool(section["allow_legacy_packages"], "allow_legacy_packages")
            for item in section.get("revoked_key_fingerprints", "").replace("\n", ",").split(","):
                item = item.strip().lower()
                if item:
                    revoked.add(item)

    if not server_url.startswith("https://"):
        # The appliance sends its API key to this server; never over plain HTTP.
        raise SecurityError(f"{path}: server_url must start with https://, got {server_url!r}")
    if not verify_tls:
        logger.warning("TLS verification is disabled in %s; use this on lab appliances only", path)

    keys = [Path(default_key_path)]
    if key_dropin_dir.is_dir():
        require_root_owned(key_dropin_dir, what="updater key directory",
                           trusted_uid=trusted_uid, stop_at=stop_at)
        keys.extend(sorted(key_dropin_dir.glob("*.pub")))
    for key in keys:
        if os.path.lexists(key):
            require_root_owned(key, what="release public key", trusted_uid=trusted_uid, stop_at=stop_at)

    return TrustSettings(
        server_url=server_url,
        verify_tls=verify_tls,
        max_manifest_age_days=max_age,
        allow_legacy_packages=allow_legacy,
        public_key_paths=tuple(str(k) for k in keys),
        revoked_key_fingerprints=frozenset(revoked),
    )
