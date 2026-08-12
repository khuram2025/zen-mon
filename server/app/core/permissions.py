"""Permission vocabulary for role-based access control.

Single source of truth for every permission the system understands,
grouped by module for the role-editor UI. Roles live in the ``roles``
table (migrate-074) as a JSONB array of these ids; ``system.admin``
implies every other permission.

LEGACY_ROLE_PERMISSIONS keeps authorization working when the roles table
is absent (tests, an appliance mid-update before the migration lands) or
when a user still carries a role name with no DB row.
"""

from __future__ import annotations

# (module id, module label, [(permission id, label, description), ...])
PERMISSION_MODULES: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    ("dashboard", "Dashboards", [
        ("dashboard.view", "View", "View the overview dashboards"),
    ]),
    ("devices", "Devices & Monitoring", [
        ("devices.view", "View", "View devices, interfaces, and monitoring data"),
        ("devices.manage", "Manage", "Add, edit, and delete devices; manage monitoring"),
    ]),
    ("discovery", "Discovery", [
        ("discovery.view", "View", "View discovery results"),
        ("discovery.run", "Run", "Run network discovery scans"),
    ]),
    ("alerts", "Alerting", [
        ("alerts.view", "View", "View alerts and alert history"),
        ("alerts.acknowledge", "Acknowledge", "Acknowledge and resolve alerts"),
        ("alerts.manage", "Manage", "Create and edit alert rules, channels, and gateways"),
    ]),
    ("service_checks", "Service Checks", [
        ("service_checks.view", "View", "View service checks and their status"),
        ("service_checks.manage", "Manage", "Create and edit service checks"),
    ]),
    ("netflow", "NetFlow", [
        ("netflow.view", "View", "View NetFlow traffic analytics"),
    ]),
    ("udt", "User Device Tracker", [
        ("udt.view", "View", "View tracked endpoints and ports"),
        ("udt.manage", "Manage", "Manage UDT settings and classification rules"),
    ]),
    ("ncm", "Config Backup (NCM)", [
        ("ncm.view", "View", "View device configuration backups"),
        ("ncm.manage", "Manage", "Run backups and manage NCM settings"),
    ]),
    ("apm", "APM & Servers", [
        ("apm.view", "View", "View APM services, traces, and server metrics"),
        ("apm.manage", "Manage", "Manage APM agents, policies, SLOs, and synthetics"),
    ]),
    ("reports", "Reports", [
        ("reports.view", "View", "View reports"),
        ("reports.export", "Export", "Export and download reports"),
        ("reports.manage", "Manage", "Create and schedule reports"),
    ]),
    ("maps", "Maps", [
        ("maps.view", "View", "View network maps"),
        ("maps.manage", "Manage", "Create and edit network maps"),
    ]),
    ("users", "Users & Roles", [
        ("users.view", "View users", "View user accounts"),
        ("users.manage", "Manage users", "Create, edit, and delete user accounts"),
        ("roles.manage", "Manage roles", "Create and edit roles and their permissions"),
    ]),
    ("audit", "Audit", [
        ("audit.view", "View", "View the audit log"),
    ]),
    ("settings", "System Settings", [
        ("settings.view", "View", "View system settings"),
        ("settings.manage", "Manage", "Change system settings (SMTP, company, storage, ...)"),
    ]),
    ("system", "System Administration", [
        ("system.admin", "Full administration",
         "Unrestricted access to everything, including updates, storage, "
         "security, credentials, and authentication providers"),
    ]),
]

ALL_PERMISSIONS: list[str] = [
    perm_id
    for _, _, perms in PERMISSION_MODULES
    for perm_id, _, _ in perms
]

_ALL_SET = set(ALL_PERMISSIONS)

SUPERUSER_PERMISSION = "system.admin"

_OPERATOR = [
    "dashboard.view", "devices.view", "devices.manage", "discovery.view",
    "discovery.run", "alerts.view", "alerts.acknowledge", "alerts.manage",
    "service_checks.view", "service_checks.manage", "netflow.view",
    "udt.view", "udt.manage", "ncm.view", "ncm.manage", "apm.view",
    "apm.manage", "reports.view", "reports.export", "maps.view",
    "maps.manage", "settings.view",
]

# Fallback when the roles table is unavailable or the role has no DB row.
# Mirrors the migrate-074 seeds; "editor" predates the roles table.
LEGACY_ROLE_PERMISSIONS: dict[str, list[str]] = {
    "admin": ALL_PERMISSIONS,
    "owner": ALL_PERMISSIONS,
    "operator": _OPERATOR,
    "editor": _OPERATOR,
    "viewer": [
        "dashboard.view", "devices.view", "discovery.view", "alerts.view",
        "alerts.acknowledge", "service_checks.view", "netflow.view",
        "udt.view", "ncm.view", "apm.view", "reports.view", "reports.export",
        "maps.view",
    ],
    "read_only": [
        "dashboard.view", "devices.view", "alerts.view",
        "service_checks.view", "netflow.view", "udt.view", "apm.view",
        "reports.view", "maps.view",
    ],
}


def is_known_permission(perm: str) -> bool:
    return perm in _ALL_SET


def has_permission(granted: list[str] | set[str], required: str) -> bool:
    return SUPERUSER_PERMISSION in granted or required in granted


def catalog() -> list[dict]:
    """Module-grouped catalog for the role-editor UI."""
    return [
        {
            "module": module_id,
            "label": label,
            "permissions": [
                {"id": pid, "label": plabel, "description": pdesc}
                for pid, plabel, pdesc in perms
            ],
        }
        for module_id, label, perms in PERMISSION_MODULES
    ]
