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
        ("alerts.manage", "Manage",
         "Create and edit alert rules, notification channels, and escalation policies"),
    ]),
    ("service_checks", "Service Checks", [
        ("service_checks.view", "View", "View service checks and their status"),
        ("service_checks.manage", "Manage", "Create and edit service checks"),
    ]),
    ("netflow", "NetFlow", [
        ("netflow.view", "View", "View NetFlow traffic analytics"),
        ("netflow.manage", "Manage",
         "Save personal NetFlow views, map flow exporters to devices and manage "
         "NetFlow settings, rules and detection exceptions"),
        ("netflow.manage_shared", "Manage shared views",
         "Create, edit, and delete NetFlow views shared with every user"),
        ("netflow.export", "Export raw", "Export complete raw flow result sets"),
    ]),
    ("udt", "User Device Tracker", [
        ("udt.view", "View", "View tracked endpoints and ports"),
        ("udt.view_users", "View user activity",
         "See which Active Directory user logged in on which device and when"),
        ("udt.manage", "Manage", "Manage UDT settings and classification rules"),
    ]),
    ("netpath", "Network Path", [
        ("netpath.view", "View", "View network path probes and hop-by-hop history"),
        ("netpath.manage", "Manage", "Create, edit, and run network path probes"),
    ]),
    ("ncm", "Config Backup (NCM)", [
        ("ncm.view", "View", "View device configuration backups"),
        ("ncm.manage", "Manage", "Run backups and manage NCM settings"),
        ("ncm.credentials", "Credentials", "Administer shared NCM connection profiles"),
        ("ncm.export", "Export raw", "Read and export configurations including secrets"),
        ("ncm.baseline", "Baselines", "Pin and approve configuration recovery points"),
        ("ncm.compliance", "Compliance", "Manage compliance policies, rules and exceptions"),
    ]),
    ("servers", "Servers & Agents", [
        ("servers.view", "View",
         "View servers, their metrics, agent fleet, agent policies and software baselines"),
        ("servers.manage", "Manage",
         "Add and edit servers; manage agents, agent policies, baselines and server diagnostics"),
    ]),
    ("apm", "Applications (APM)", [
        ("apm.view", "View", "View APM services, service map, traces, errors, SLOs, synthetics and RUM"),
        ("apm.manage", "Manage", "Manage APM ingest keys, SLOs, synthetics, RUM applications and APM settings"),
    ]),
    ("inventory", "Inventory", [
        ("inventory.view", "View",
         "View OS, firmware, software and hardware inventory for devices and servers "
         "within your tag scope"),
        ("inventory.export", "Export",
         "Export inventory datasets to CSV (serial numbers and software lists are "
         "reconnaissance data)"),
        ("inventory.manage", "Manage",
         "Manage software catalog rules, asset attributes, lifecycle overrides, "
         "shared saved views and inventory settings"),
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
        ("users.manage", "Manage users",
         "Create, edit, disable, and delete user accounts up to your own access level; "
         "reset passwords and revoke sessions"),
        ("roles.view", "View roles", "View roles, their permissions, and their members"),
        ("roles.manage", "Manage roles",
         "Create and edit roles up to your own access level (you cannot grant what you lack)"),
    ]),
    ("audit", "Audit", [
        ("audit.view", "View", "Read the audit log (does not include any administration rights)"),
    ]),
    ("settings", "System Settings", [
        ("settings.view", "View", "View system settings"),
        ("settings.manage", "Manage",
         "Change company, monitoring-threshold, and SMTP/SMS gateway settings. "
         "Security, storage, updates, and authentication providers need Full administration"),
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

# Risk classification shown in the role editor and used for confirmation /
# reason prompts. "standard" permissions are not listed here.
#   sensitive  - exposes data most operators should not read by default
#   privileged - changes who can do what, or changes shared infrastructure
#   critical   - unrestricted control of the appliance
PERMISSION_RISK: dict[str, str] = {
    "audit.view": "sensitive",
    "udt.view_users": "sensitive",
    "ncm.export": "sensitive",
    "netflow.export": "sensitive",
    "inventory.export": "sensitive",
    "ncm.credentials": "privileged",
    "users.manage": "privileged",
    "roles.manage": "privileged",
    "settings.manage": "privileged",
    "netflow.manage_shared": "privileged",
    SUPERUSER_PERMISSION: "critical",
}

# Permissions that make a role able to change access for other people. A role
# holding any of these must never be editable by an actor who lacks them.
ACCESS_CONTROL_PERMISSIONS = frozenset({"users.manage", "roles.manage", SUPERUSER_PERMISSION})

# Intentional dependencies: the editor offers to add the right-hand side when
# the left-hand permission is selected. Enforcement stays explicit (no hidden
# grants), so a role is always exactly what the administrator saved.
PERMISSION_IMPLIES: dict[str, list[str]] = {
    "devices.manage": ["devices.view"],
    "discovery.run": ["discovery.view"],
    "alerts.acknowledge": ["alerts.view"],
    "alerts.manage": ["alerts.view"],
    "service_checks.manage": ["service_checks.view"],
    "netflow.manage": ["netflow.view"],
    "netflow.manage_shared": ["netflow.manage", "netflow.view"],
    "netflow.export": ["netflow.view"],
    "udt.view_users": ["udt.view"],
    "udt.manage": ["udt.view"],
    "netpath.manage": ["netpath.view"],
    "ncm.manage": ["ncm.view"],
    "ncm.credentials": ["ncm.view"],
    "ncm.export": ["ncm.view"],
    "ncm.baseline": ["ncm.view"],
    "ncm.compliance": ["ncm.view"],
    "servers.manage": ["servers.view"],
    "apm.manage": ["apm.view"],
    "inventory.manage": ["inventory.view"],
    "inventory.export": ["inventory.view"],
    "reports.export": ["reports.view"],
    "reports.manage": ["reports.view"],
    "maps.manage": ["maps.view"],
    "users.manage": ["users.view", "roles.view"],
    "roles.manage": ["roles.view"],
    "settings.manage": ["settings.view"],
}

_OPERATOR = [
    "dashboard.view", "devices.view", "devices.manage", "discovery.view",
    "discovery.run", "alerts.view", "alerts.acknowledge", "alerts.manage",
    "service_checks.view", "service_checks.manage", "netflow.view",
    "netflow.manage", "netflow.manage_shared", "netflow.export",
    "udt.view", "udt.view_users", "udt.manage", "netpath.view", "netpath.manage",
    "ncm.view", "ncm.manage", "servers.view", "servers.manage", "apm.view",
    "apm.manage", "inventory.view", "inventory.export", "inventory.manage",
    "reports.view", "reports.export", "reports.manage", "maps.view",
    "maps.manage", "settings.view",
]

_VIEWER = [
    "dashboard.view", "devices.view", "discovery.view", "alerts.view",
    "alerts.acknowledge", "service_checks.view", "netflow.view",
    "udt.view", "netpath.view", "ncm.view", "servers.view", "apm.view", "inventory.view",
    "reports.view", "reports.export", "maps.view",
]

_READ_ONLY = [
    "dashboard.view", "devices.view", "alerts.view",
    "service_checks.view", "netflow.view", "udt.view", "netpath.view",
    "servers.view", "apm.view", "inventory.view", "reports.view", "maps.view",
]

# Team roles: least-privilege starting points for the teams that share the
# appliance. Each sees only its own area of the product; pair one with a
# visibility scope (tags on a custom copy of the role, or on the user) to
# limit it to that team's resources as well.
_NETWORK_TEAM = [
    "dashboard.view", "devices.view", "devices.manage", "discovery.view", "discovery.run",
    "alerts.view", "alerts.acknowledge", "service_checks.view", "netflow.view",
    "udt.view", "netpath.view", "netpath.manage", "ncm.view", "ncm.manage", "ncm.baseline",
    "inventory.view", "reports.view", "reports.export", "maps.view", "maps.manage",
]

_SYSTEMS_TEAM = [
    "dashboard.view", "servers.view", "servers.manage", "service_checks.view",
    "service_checks.manage", "alerts.view", "alerts.acknowledge", "inventory.view",
    "reports.view", "reports.export",
]

_APPLICATION_TEAM = [
    "dashboard.view", "apm.view", "apm.manage", "service_checks.view", "service_checks.manage",
    "alerts.view", "alerts.acknowledge", "reports.view", "reports.export",
]

_SECURITY_TEAM = [
    "dashboard.view", "devices.view", "alerts.view", "alerts.acknowledge", "netflow.view",
    "netflow.export", "udt.view", "udt.view_users", "ncm.view", "ncm.compliance", "audit.view",
    "inventory.view", "reports.view", "reports.export",
]

# Version of the built-in role definitions below. Persisted on is_system role
# rows (roles.definition_version); when the code's version is newer the
# built-in rows are reconciled at startup. Custom roles are NEVER touched, so a
# newly introduced permission is never granted to a custom role implicitly.
#   4 - servers.* split out of apm.*; built-in team roles added.
#   5 - inventory.* added (operator: view, export, manage; viewer and read_only: view).
#       The team roles deliberately did not get inventory.* yet; see
#       Documentation/Inventory (decision D9).
#   6 - inventory.view added to network_team, systems_team and security_team (D9: view only,
#       never export or manage); application_team gets no inventory.* because it sees
#       applications, not the estate.
BUILTIN_ROLE_DEFINITION_VERSION = 6

BUILTIN_ROLE_DEFINITIONS: dict[str, list[str]] = {
    "admin": list(ALL_PERMISSIONS),
    "operator": _OPERATOR,
    "viewer": _VIEWER,
    "read_only": _READ_ONLY,
    "network_team": _NETWORK_TEAM,
    "systems_team": _SYSTEMS_TEAM,
    "application_team": _APPLICATION_TEAM,
    "security_team": _SECURITY_TEAM,
}

# Display name and description used when a built-in role row is created at
# startup. The four original built-ins were seeded by migrate-074 and are only
# reconciled (permissions), never renamed.
BUILTIN_ROLE_META: dict[str, tuple[str, str]] = {
    "network_team": (
        "Network Team",
        "Network devices, discovery, NetFlow, NetPath, Device Tracker, configuration backups and maps. "
        "Includes read access to Inventory (inventory.view).",
    ),
    "systems_team": (
        "Systems Team",
        "Servers, agents, agent policies and baselines, plus the service checks that watch them. "
        "Includes read access to Inventory (inventory.view).",
    ),
    "application_team": (
        "Application Team",
        "Application performance (APM): services, traces, errors, SLOs, synthetics and RUM, plus service checks.",
    ),
    "security_team": (
        "Security Team",
        "Read-mostly security view: NetFlow (with raw export), user-to-device activity, "
        "configuration compliance and the audit log. Includes read access to Inventory (inventory.view).",
    ),
}

# Fallback when the roles table is unavailable or the role has no DB row.
# Mirrors the built-ins; "editor" and "owner" are pre-RBAC aliases kept only so
# a mid-update appliance keeps working. They are reserved: a custom role can
# never take these names (see RESERVED_ROLE_NAMES).
LEGACY_ROLE_PERMISSIONS: dict[str, list[str]] = {
    "admin": ALL_PERMISSIONS,
    "owner": ALL_PERMISSIONS,
    "operator": _OPERATOR,
    "editor": _OPERATOR,
    "viewer": _VIEWER,
    "read_only": _READ_ONLY,
}

# Names a custom role may not use: built-ins, pre-RBAC aliases and words that
# read as an authority level. Authorization never depends on a role *name*;
# this only prevents a custom role being mistaken for (or colliding with) one.
RESERVED_ROLE_NAMES = frozenset({
    "admin", "administrator", "owner", "root", "superuser", "super_admin",
    "system", "operator", "editor", "viewer", "read_only", "readonly",
    "guest", "anonymous", "service", "agent", "sensor",
    *BUILTIN_ROLE_DEFINITIONS,
})


def is_known_permission(perm: str) -> bool:
    return perm in _ALL_SET


def has_permission(granted: list[str] | set[str] | frozenset[str], required: str) -> bool:
    return SUPERUSER_PERMISSION in granted or required in granted


def effective_permissions(granted) -> frozenset[str]:
    """The concrete permission set a role grants: ``system.admin`` expands to
    the whole vocabulary."""
    granted = set(granted or [])
    if SUPERUSER_PERMISSION in granted:
        return frozenset(_ALL_SET)
    return frozenset(p for p in granted if p in _ALL_SET)


def exceeds(target, ceiling) -> list[str]:
    """Permissions in ``target`` that ``ceiling`` does not hold. An empty list
    means the target is within the ceiling (an actor may grant / manage it)."""
    return sorted(effective_permissions(target) - effective_permissions(ceiling))


def risk_of(perm: str) -> str:
    return PERMISSION_RISK.get(perm, "standard")


# Product area each module belongs to, so the role editor can group modules
# the way teams think about them (and the way the sidebar is organised).
MODULE_AREAS: dict[str, str] = {
    "dashboard": "General",
    "service_checks": "Operations",
    "alerts": "Operations",
    "reports": "Operations",
    # Inventory spans network devices and servers; Operations is where the other
    # cross-cutting modules (alerts, reports) sit in the role editor.
    "inventory": "Operations",
    "devices": "Network",
    "discovery": "Network",
    "netflow": "Network",
    "netpath": "Network",
    "udt": "Network",
    "ncm": "Network",
    "maps": "Network",
    "servers": "Systems",
    "apm": "Applications",
    "users": "Administration",
    "audit": "Administration",
    "settings": "Administration",
    "system": "Administration",
}


def catalog() -> list[dict]:
    """Module-grouped catalog for the role-editor UI."""
    return [
        {
            "module": module_id,
            "label": label,
            "area": MODULE_AREAS.get(module_id, "Other"),
            "permissions": [
                {
                    "id": pid,
                    "label": plabel,
                    "description": pdesc,
                    "risk": risk_of(pid),
                    "implies": PERMISSION_IMPLIES.get(pid, []),
                }
                for pid, plabel, pdesc in perms
            ],
        }
        for module_id, label, perms in PERMISSION_MODULES
    ]
