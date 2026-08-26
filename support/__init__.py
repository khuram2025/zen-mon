"""ZenPlus appliance tech-support bundle generator.

Runs as the unprivileged ``zenplus`` account behind a systemd path-triggered
queue. It collects logs, configuration, health, reachability, Server-agent,
APM, and database diagnostics into one redacted archive that customers can
attach to a support ticket.

This module is intentionally independent of the FastAPI/SQLAlchemy stack so
the isolated worker stays small and starts quickly.
"""

__version__ = "1.1.0"
BUNDLE_SCHEMA_VERSION = 2
