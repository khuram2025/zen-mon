"""Agent configuration loader."""

import configparser
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .trust import TrustSettings, load_trust

logger = logging.getLogger("zenplus.updater")

UPDATER_DIR = Path("/opt/zenplus/updater")
DEFAULT_CONFIG_PATH = UPDATER_DIR / "config" / "agent.conf"
SUBSCRIPTION_PATH = UPDATER_DIR / "config" / "subscription.json"


@dataclass
class ServerConfig:
    url: str = "https://zentryc.com"
    check_interval_seconds: int = 900
    download_timeout_seconds: int = 600


@dataclass
class ApplianceConfig:
    id: str = ""
    api_key: str = ""


@dataclass
class SecurityConfig:
    """What the updater trusts. Filled from root-owned settings, never agent.conf."""

    public_key_path: str = str(UPDATER_DIR / "keys" / "zentryc-release.pub")
    public_key_paths: tuple = (str(UPDATER_DIR / "keys" / "zentryc-release.pub"),)
    revoked_key_fingerprints: frozenset = frozenset()
    max_manifest_age_days: int = 30
    verify_tls: bool = True
    allow_legacy_packages: bool = False


@dataclass
class UpdateConfig:
    backup_dir: str = "/var/lib/zenplus-updater/backups"
    max_backups: int = 3
    auto_update: bool = True
    maintenance_window_start: str = ""
    maintenance_window_end: str = ""


@dataclass
class LoggingConfig:
    log_file: str = str(UPDATER_DIR / "logs" / "update.log")
    log_level: str = "INFO"
    max_log_size_mb: int = 10
    log_rotate_count: int = 5


@dataclass
class AgentConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    appliance: ApplianceConfig = field(default_factory=ApplianceConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _apply_trust(cfg: AgentConfig, trust: TrustSettings, parser: configparser.ConfigParser | None) -> None:
    """Overwrite every trust-relevant field with the root-owned values.

    agent.conf is writable by the app account, so a differing value there is
    ignored (and reported) instead of being honoured.
    """
    if parser is not None:
        ignored = []
        file_url = parser.get("server", "url", fallback=None)
        if file_url is not None and file_url.strip() != trust.server_url:
            ignored.append("[server] url")
        for key, effective in (
            ("public_key_path", trust.public_key_paths[0] if trust.public_key_paths else ""),
            ("max_manifest_age_days", str(trust.max_manifest_age_days)),
            ("verify_tls", str(trust.verify_tls)),
        ):
            file_value = parser.get("security", key, fallback=None)
            if file_value is None:
                continue
            if key == "verify_tls":
                if file_value.strip().lower() != effective.lower():
                    ignored.append(f"[security] {key}")
            elif file_value.strip() != effective:
                ignored.append(f"[security] {key}")
        if ignored:
            logger.warning(
                "Ignoring %s in agent.conf: updater trust settings are read only from "
                "/etc/zenplus/updater-trust.conf",
                ", ".join(ignored),
            )

    cfg.server.url = trust.server_url
    cfg.security.verify_tls = trust.verify_tls
    cfg.security.max_manifest_age_days = trust.max_manifest_age_days
    cfg.security.allow_legacy_packages = trust.allow_legacy_packages
    cfg.security.public_key_paths = trust.public_key_paths
    cfg.security.public_key_path = trust.public_key_paths[0]
    cfg.security.revoked_key_fingerprints = trust.revoked_key_fingerprints


def load_config(path: str | None = None, *, trust: TrustSettings | None = None) -> AgentConfig:
    """Load agent configuration from INI file.

    Enrollment state and operational settings come from agent.conf. Trust
    settings (server URL, TLS verification, release keys, manifest age) come
    from root-owned files only; see updater/trust.py.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = AgentConfig()
    if trust is None:
        trust = load_trust()

    if not config_path.exists():
        _apply_trust(cfg, trust, None)
        return cfg

    parser = configparser.ConfigParser()
    parser.read(config_path)

    if parser.has_section("server"):
        s = parser["server"]
        cfg.server.check_interval_seconds = s.getint(
            "check_interval_seconds", cfg.server.check_interval_seconds
        )
        cfg.server.download_timeout_seconds = s.getint(
            "download_timeout_seconds", cfg.server.download_timeout_seconds
        )

    if parser.has_section("appliance"):
        s = parser["appliance"]
        cfg.appliance.id = s.get("id", cfg.appliance.id)
        cfg.appliance.api_key = s.get("api_key", cfg.appliance.api_key)

    if parser.has_section("update"):
        s = parser["update"]
        # Paths and retention govern privileged writes/deletions. They are
        # compiled policy, not values delegated to the application account.
        cfg.update.auto_update = s.getboolean("auto_update", cfg.update.auto_update)
        cfg.update.maintenance_window_start = s.get(
            "maintenance_window_start", cfg.update.maintenance_window_start
        )
        cfg.update.maintenance_window_end = s.get(
            "maintenance_window_end", cfg.update.maintenance_window_end
        )

    if parser.has_section("logging"):
        s = parser["logging"]
        cfg.logging.log_level = s.get("log_level", cfg.logging.log_level)

    for section, names in (("update", ("backup_dir", "max_backups")),
                           ("logging", ("log_file", "max_log_size_mb", "log_rotate_count"))):
        if any(parser.has_option(section, name) for name in names):
            logger.warning("Ignoring privileged path/retention settings in [%s] of agent.conf", section)

    _apply_trust(cfg, trust, parser)
    return cfg


def save_config(cfg: AgentConfig, path: str | None = None) -> None:
    """Save agent configuration to INI file."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)

    parser = configparser.ConfigParser()

    # Trust settings (server URL, TLS, release keys, manifest age) are not
    # persisted here: this file is writable by the app account and the loader
    # ignores them. See updater/trust.py.
    parser["server"] = {
        "check_interval_seconds": str(cfg.server.check_interval_seconds),
        "download_timeout_seconds": str(cfg.server.download_timeout_seconds),
    }

    parser["appliance"] = {
        "id": cfg.appliance.id,
        "api_key": cfg.appliance.api_key,
    }

    parser["update"] = {
        "auto_update": str(cfg.update.auto_update),
        "maintenance_window_start": cfg.update.maintenance_window_start,
        "maintenance_window_end": cfg.update.maintenance_window_end,
    }

    parser["logging"] = {
        "log_level": cfg.logging.log_level,
    }

    with open(config_path, "w") as f:
        parser.write(f)

    # Secure the config file (contains api_key)
    os.chmod(config_path, 0o600)


def save_subscription(data: dict | None) -> None:
    """Persist subscription data from the OTA server to a local JSON file."""
    if data is None:
        return
    SUBSCRIPTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SUBSCRIPTION_PATH, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(SUBSCRIPTION_PATH, 0o600)


def load_subscription() -> dict | None:
    """Load cached subscription data from the local JSON file."""
    if not SUBSCRIPTION_PATH.exists():
        return None
    try:
        with open(SUBSCRIPTION_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
