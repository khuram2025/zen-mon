"""ZenPlus appliance tech-support bundle generator.

Runs as a root systemd template unit, invoked by the API process via a narrow
sudoers grant. Collects logs, configuration, health, and database/clickhouse
diagnostics into a single redacted tarball that customers can attach to a
support ticket.

This module is intentionally kept independent of the FastAPI/SQLAlchemy stack
so the root worker can stay small and start fast.
"""

__version__ = "1.0.0"
BUNDLE_SCHEMA_VERSION = 1
