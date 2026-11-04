# ZenPlus 1.25.0

Add native virtualization connectors for Nutanix Prism Central v4, Prism Element
v2, VMware vSphere and Hyper-V through a restricted JEA endpoint. Discover
clusters, hosts, VMs and storage; collect supported capacity, utilization and
health metrics, retain history and show provider alerts with resource links.
Unavailable metrics and unsupported API capabilities remain explicitly unknown.

Connections require certificate and hostname validation, encrypted credentials,
and a successful test before saving. Reading requires servers.view; configuring
connectors requires system.admin. Existing user and role tag scopes apply.
All Servers and Virtualization link to each other under Servers.

PostgreSQL migration 171 adds connector configuration and last successful
snapshots. ClickHouse migration 172 adds 30-day metric history. Existing agent
inventory and guest OS monitoring continue independently. See the
[setup guide](../Documentation/24-VIRTUALIZATION-MONITORING.md) for provider
versions, permissions, JEA installation, TLS trust and SNIH firewall scope.

Appliance-only release, requiring the signed 1.24.36 bridge. Canary target:
192.168.8.221 only, with automatic promotion disabled. Full rollout requires
successful canary observation and tests on other appliances, including the
1.24.29 → 1.24.36 → 1.25.0 upgrade path. Live SNIH Prism validation still needs
the scoped TCP 9440 rule, trusted certificate and read-only credential.
