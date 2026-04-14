"""Agent configuration loader."""

import configparser
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

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
    public_key_path: str = str(UPDATER_DIR / "keys" / "zentryc-release.pub")
    max_manifest_age_days: int = 30
    verify_tls: bool = True


@dataclass
class UpdateConfig:
    backup_dir: str = str(UPDATER_DIR / "backups")
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


def load_config(path: str | None = None) -> AgentConfig:
    """Load agent configuration from INI file."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = AgentConfig()

    if not config_path.exists():
        return cfg

    parser = configparser.ConfigParser()
    parser.read(config_path)

    if parser.has_section("server"):
        s = parser["server"]
        cfg.server.url = s.get("url", cfg.server.url)
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

    if parser.has_section("security"):
        s = parser["security"]
        cfg.security.public_key_path = s.get(
            "public_key_path", cfg.security.public_key_path
        )
        cfg.security.max_manifest_age_days = s.getint(
            "max_manifest_age_days", cfg.security.max_manifest_age_days
        )
        cfg.security.verify_tls = s.getboolean("verify_tls", cfg.security.verify_tls)

    if parser.has_section("update"):
        s = parser["update"]
        cfg.update.backup_dir = s.get("backup_dir", cfg.update.backup_dir)
        cfg.update.max_backups = s.getint("max_backups", cfg.update.max_backups)
        cfg.update.auto_update = s.getboolean("auto_update", cfg.update.auto_update)
        cfg.update.maintenance_window_start = s.get(
            "maintenance_window_start", cfg.update.maintenance_window_start
        )
        cfg.update.maintenance_window_end = s.get(
            "maintenance_window_end", cfg.update.maintenance_window_end
        )

    if parser.has_section("logging"):
        s = parser["logging"]
        cfg.logging.log_file = s.get("log_file", cfg.logging.log_file)
        cfg.logging.log_level = s.get("log_level", cfg.logging.log_level)
        cfg.logging.max_log_size_mb = s.getint(
            "max_log_size_mb", cfg.logging.max_log_size_mb
        )
        cfg.logging.log_rotate_count = s.getint(
            "log_rotate_count", cfg.logging.log_rotate_count
        )

    return cfg


def save_config(cfg: AgentConfig, path: str | None = None) -> None:
    """Save agent configuration to INI file."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)

    parser = configparser.ConfigParser()

    parser["server"] = {
        "url": cfg.server.url,
        "check_interval_seconds": str(cfg.server.check_interval_seconds),
        "download_timeout_seconds": str(cfg.server.download_timeout_seconds),
    }

    parser["appliance"] = {
        "id": cfg.appliance.id,
        "api_key": cfg.appliance.api_key,
    }

    parser["security"] = {
        "public_key_path": cfg.security.public_key_path,
        "max_manifest_age_days": str(cfg.security.max_manifest_age_days),
        "verify_tls": str(cfg.security.verify_tls),
    }

    parser["update"] = {
        "backup_dir": cfg.update.backup_dir,
        "max_backups": str(cfg.update.max_backups),
        "auto_update": str(cfg.update.auto_update),
        "maintenance_window_start": cfg.update.maintenance_window_start,
        "maintenance_window_end": cfg.update.maintenance_window_end,
    }

    parser["logging"] = {
        "log_file": cfg.logging.log_file,
        "log_level": cfg.logging.log_level,
        "max_log_size_mb": str(cfg.logging.max_log_size_mb),
        "log_rotate_count": str(cfg.logging.log_rotate_count),
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
