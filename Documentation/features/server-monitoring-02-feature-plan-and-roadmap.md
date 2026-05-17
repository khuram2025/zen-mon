# Server Monitoring Feature Plan And Roadmap

Date: 2026-05-15

## Product Goal

Make servers first-class monitored entities in ZenPlus Monitor. A server can be monitored by:

- A per-host Windows agent.
- A per-host Linux agent later.
- Agentless checks executed by a remote sensor through WMI, WinRM, SNMP, SSH, or vendor protocols.

The product should support server infrastructure first and then extend naturally into application-level monitoring.

## Core Concepts

### Server

A server is a monitored host with identity, OS details, site, tags, owner, lifecycle, collection mode, and relationships to devices, agents, services, applications, and alerts.

Recommended fields:

- `id`
- `display_name`
- `hostname`
- `fqdn`
- `primary_ip`
- `site_id`
- `device_id`
- `os_type`
- `os_name`
- `os_version`
- `kernel_or_build`
- `architecture`
- `collection_mode`
- `status`
- `last_seen`
- `tags`
- `owner`
- `environment`
- `created_at`
- `updated_at`

### Agent

An agent is an installed local service on a server.

Recommended fields:

- `id`
- `server_id`
- `agent_uid`
- `hostname`
- `platform`
- `version`
- `install_id`
- `site_id`
- `policy_id`
- `status`
- `last_heartbeat_at`
- `last_config_hash`
- `last_metric_at`
- `queue_depth`
- `spool_bytes`
- `update_ring`
- `desired_version`
- `current_version`
- `certificate_expires_at`
- `created_at`
- `updated_at`

### Policy

A policy defines what is collected, how often, and with what limits.

Policy examples:

- Windows baseline.
- Windows high detail.
- Windows SQL Server.
- Windows IIS.
- Linux baseline.
- Linux container host.
- Agentless Windows baseline.

### Remote Sensor

The current sensor appliance remains the execution point for private-network polling and agentless monitoring. It should not be renamed or replaced by the server agent. A sensor monitors many things from a site. An agent monitors the host on which it is installed.

## Collection Modes

### Windows Agent MVP

This is the first build target.

Telemetry:

- Heartbeat and agent health.
- CPU utilization total and per-core.
- Memory total, used, available, committed.
- Disk capacity and usage.
- Disk IO counters.
- Filesystem mount inventory.
- Network interface inventory, bytes, packets, errors, drops.
- Uptime, boot time, time drift if available.
- OS name, version, build, architecture.
- Hardware inventory: CPU model/count, memory size, disks.
- Process summary: top CPU, top memory, process count.
- Configurable process watchlist.
- Windows service state for selected services and auto-start failures.
- Windows Event Log summary for System/Application errors and warnings.
- Agent queue depth, spool size, config hash, version, update status.

MVP exclusions:

- Full log streaming.
- APM/tracing.
- Endpoint security scanning.
- Remote shell.
- Automatic code injection into applications.

### Agentless Windows

Build after the agent MVP is working.

Execution point:

- Remote sensor inside the customer site.

Protocols:

- WMI for common host metrics.
- WinRM/PowerShell for richer checks and inventory where allowed.
- SNMP for lightweight fallback.

Telemetry:

- CPU, memory, disk, filesystem, network.
- Service state.
- Process list summary.
- Event log query for critical/error counts.
- Uptime and OS inventory.

Required backend support:

- Credential vault records.
- Sensor-side credential assignment.
- Per-check timeout and concurrency limits.
- Clear warnings when WMI/WinRM permissions are insufficient.

### Linux Agent

Build after Windows agent and host ingestion are stable.

Telemetry:

- CPU, memory, load, disk, filesystem, network.
- systemd service states.
- Process and container summary.
- Kernel, distro, package inventory.
- Journal summary.
- Agent health and local queue.

Packaging:

- `.deb`
- `.rpm`
- optional tarball for restricted systems.

### Agentless Linux

Build after remote sensor credential vaulting is ready.

Protocols:

- SSH.
- SNMP.

Telemetry:

- Baseline CPU, memory, disk, filesystem, network, service/process checks, uptime, OS inventory.

### Application-Level Extension

The server agent should be designed as the base for application monitoring.

Initial app-level path:

- Discover listening ports and owning processes.
- Discover Windows services and Linux systemd services.
- Add templates for IIS, SQL Server, Active Directory, DNS, DHCP, file server, Hyper-V, NGINX, PostgreSQL, MySQL, Redis, Docker.
- Add process/service health checks and performance counters.
- Add log tailing and event filters.
- Add OpenTelemetry Collector compatible ingestion for traces and metrics later.

## Backend Architecture

### New REST APIs

Agent runtime APIs:

- `POST /api/v1/agents/enroll`
- `POST /api/v1/agents/heartbeat`
- `GET /api/v1/agents/config`
- `POST /api/v1/agents/results/host`
- `POST /api/v1/agents/events`
- `POST /api/v1/agents/diagnostics`
- `GET /api/v1/agents/packages/manifest`
- `GET /api/v1/agents/packages/{platform}/{version}`
- `POST /api/v1/agents/commands/poll`
- `POST /api/v1/agents/commands/{id}/result`

Admin APIs:

- `GET /api/v1/servers`
- `POST /api/v1/servers`
- `GET /api/v1/servers/{id}`
- `PATCH /api/v1/servers/{id}`
- `GET /api/v1/servers/{id}/metrics`
- `GET /api/v1/servers/{id}/processes`
- `GET /api/v1/servers/{id}/services`
- `GET /api/v1/servers/{id}/events`
- `GET /api/v1/servers/{id}/agent`
- `POST /api/v1/servers/{id}/install-token`
- `POST /api/v1/servers/{id}/decommission`
- `GET /api/v1/agent-policies`
- `POST /api/v1/agent-policies`
- `PATCH /api/v1/agent-policies/{id}`
- `GET /api/v1/agent-fleet`
- `POST /api/v1/agent-fleet/{agent_id}/rotate-certificate`
- `POST /api/v1/agent-fleet/{agent_id}/request-diagnostics`
- `POST /api/v1/agent-fleet/{agent_id}/set-update-ring`

### Relational Tables

Add these tables through migrations:

- `servers`
- `server_tags`
- `agents`
- `agent_policies`
- `agent_policy_assignments`
- `agent_enrollment_tokens`
- `agent_packages`
- `agent_commands`
- `agent_command_results`
- `agent_diagnostics`
- `server_credentials` for agentless use, encrypted.
- `server_service_inventory`
- `server_process_inventory`
- `server_filesystem_inventory`
- `server_network_interface_inventory`
- `server_software_inventory`

Design notes:

- Keep enrollment token hashes only.
- Keep secrets encrypted through the existing app secret system or a dedicated vault integration.
- Store current inventory in Postgres for UI lookup.
- Store time-series values in ClickHouse.

### ClickHouse Tables

Add time-series tables:

- `host_metrics`
- `host_cpu_metrics`
- `host_memory_metrics`
- `host_filesystem_metrics`
- `host_disk_io_metrics`
- `host_network_metrics`
- `host_process_metrics`
- `host_service_state`
- `host_event_log_summary`
- `agent_health_metrics`

Add rollups:

- 1 minute.
- 5 minute.
- 1 hour.
- 1 day.

Retention:

- Raw high-cardinality process metrics: short retention, for example 7 to 14 days.
- Raw host metrics: 30 to 90 days depending license/edition.
- Rollups: 1 year or more.

### Result Contract

Use batch upload with idempotency.

Required envelope fields:

- `agent_id`
- `server_id`
- `batch_id`
- `sequence_start`
- `sequence_end`
- `config_hash`
- `agent_version`
- `collected_at`
- `sent_at`
- `metrics`
- `inventory`
- `events`

Backend behavior:

- Reject unknown or disabled agents.
- Deduplicate by `agent_id` plus sequence or batch id.
- Return accepted/rejected counts.
- Return backpressure hints when overloaded.

### Configuration Contract

The config sent to agents must include:

- Config version.
- Policy id.
- Collection intervals.
- Enabled collectors.
- Process and service watchlists.
- Event log filters.
- Cardinality limits.
- Batch size limits.
- Upload interval.
- Update policy.
- Feature flags.
- Signature.

Agent behavior:

- Verify config signature before applying.
- Keep last known good config.
- Reject incompatible config versions.
- Report config apply errors in heartbeat.

## Security Model

### Enrollment

Recommended flow:

1. Admin creates an install token from the UI.
2. Token is scoped to site, policy, platform, expiry, and optional hostname.
3. Token is shown once and stored only as a hash in the backend.
4. MSI writes token into a bootstrap file or passes it to the service on first start.
5. Agent exchanges token for an agent identity and client credential.
6. Agent stores the credential using Windows DPAPI.
7. Agent removes the bootstrap token from disk.

### Transport

Minimum:

- HTTPS with TLS verification enabled by default.
- Proxy support.
- Certificate pinning optional for controlled enterprise environments.

Target:

- mTLS for agent-controller communication.
- Automatic certificate rotation before expiry.

### Updates

Required:

- Signed package manifest.
- Authenticode signed Windows executable and MSI.
- SHA256 checksum displayed in UI.
- Version pinning and update rings.
- Rollback after failed health check.

### Command Channel

Default:

- Disabled for arbitrary execution.

Allowed commands:

- `status`
- `collect_now`
- `refresh_config`
- `upload_diagnostics`
- `rotate_certificate`
- `restart_agent`
- `upgrade_agent`

Controls:

- RBAC.
- Audit log.
- Expiry.
- Agent allowlist.
- No unrestricted remote shell in the server monitoring MVP.

## UI Plan

### Server Overview

Purpose:

- Show fleet health and resource pressure quickly.

Sections:

- Total servers.
- Healthy, warning, critical, unknown.
- Agent online/offline/stale.
- OS breakdown.
- Sites with server counts.
- Top CPU pressure.
- Top memory pressure.
- Top disk pressure.
- Top network throughput.
- Recent critical events.
- Recent agent update failures.

Controls:

- Site filter.
- OS filter.
- Collection mode filter.
- Status filter.
- Tags filter.
- Search by hostname, IP, owner, environment.

### Add Server Flow

Steps:

1. Choose collection mode.
2. Choose site and policy.
3. For agent mode, generate MSI or command.
4. For agentless mode, choose remote sensor and credentials.
5. Show validation status and first telemetry.

Collection choices:

- Windows Agent, recommended.
- Windows Agentless via WMI/WinRM.
- SNMP.
- Linux Agent, later.
- Linux SSH, later.

### Server Detail

Tabs:

- Overview.
- Metrics.
- Filesystems.
- Network.
- Processes.
- Services.
- Events.
- Inventory.
- Agent.
- Alerts.
- Timeline.

Overview content:

- Status strip.
- Host identity.
- Agent status.
- CPU/memory/disk/network charts.
- Top processes.
- Critical service states.
- Event log error summary.
- Recent alerts.

Agent tab:

- Version.
- Policy.
- Config hash.
- Last heartbeat.
- Queue depth.
- Spool size.
- Last upload.
- Update ring.
- Certificate expiry.
- Diagnostics actions.

### Agent Fleet

Purpose:

- Operate agents at scale.

Columns:

- Hostname.
- Site.
- OS.
- Agent version.
- Status.
- Last heartbeat.
- Queue depth.
- Spool size.
- Policy.
- Config hash.
- Update ring.
- Desired version.
- Certificate expiry.

Bulk actions:

- Change policy.
- Change update ring.
- Request diagnostics.
- Rotate certificate.
- Trigger upgrade.
- Disable/decommission.

### Policy Builder

Controls:

- Metric collection interval.
- Upload interval.
- Process top-N.
- Service watchlist.
- Event log filters.
- Disk ignore patterns.
- Network interface ignore patterns.
- Cardinality limits.
- Update ring.
- Feature flags.

### Alert Templates

Initial templates:

- Server down or stale.
- CPU sustained high.
- Memory pressure.
- Disk space low.
- Disk IO saturation.
- Network errors.
- Windows service stopped.
- Event log critical spike.
- Agent queue growing.
- Agent config apply failed.
- Agent update failed.

## Roadmap

### Phase 0: Contracts And Design

Deliverables:

- Final server entity model.
- Agent runtime API contract.
- ClickHouse schema design.
- Windows agent architecture.
- MSI packaging design.
- UI wireframes.
- Security threat model.

### Phase 1: Windows Agent MVP

Deliverables:

- Agent enrollment.
- Heartbeat.
- Signed config download.
- CPU, memory, filesystem, disk IO, network, uptime, OS inventory.
- Durable local spool.
- Batch upload.
- Windows service install through MSI.
- Package manifest and download API.
- Server overview and server detail MVP.
- Basic alert templates.

### Phase 2: Fleet Reliability And Security

Deliverables:

- mTLS.
- Certificate rotation.
- Signed package updates.
- Update rings.
- Rollback.
- Agent diagnostics bundle.
- Fleet operations UI.
- Backpressure and load tests.
- Audit log coverage.

### Phase 3: Agentless Windows And Linux Agent

Deliverables:

- WMI/WinRM checks through remote sensor.
- Credential vault and assignment UI.
- Linux agent service.
- `.deb` and `.rpm` packaging.
- Linux server UI parity.

### Phase 4: Application Monitoring Foundation

Deliverables:

- Process/service discovery.
- IIS template.
- SQL Server template.
- Active Directory template.
- Linux NGINX/PostgreSQL/MySQL templates.
- Log/event ingestion controls.
- OpenTelemetry-compatible ingestion plan.

### Phase 5: Advanced Correlation

Deliverables:

- Dynamic baselines.
- Dependency-aware alert suppression.
- Server/application topology map.
- Incident timeline correlation.
- Root-cause suggestions.
- Capacity forecasting.

## Initial MVP Acceptance Criteria

The Windows agent MVP is acceptable when:

- A user can create an install token from the UI.
- A user can install the MSI silently on Windows.
- The installed service enrolls and starts automatically.
- The agent appears in fleet status within 60 seconds.
- CPU, memory, disk, filesystem, network, uptime, and OS inventory appear in the server detail page.
- Agent metrics continue after a service restart.
- The agent queues data during a network outage and uploads later.
- The UI clearly marks stale agents.
- Backend rejects invalid, expired, or reused enrollment tokens.
- Package checksum and version are visible.
- Basic alerts can be created for stale agent, high CPU, high memory, and low disk.

## Non-Functional Targets

Agent:

- CPU: below 2 percent average on a normal server.
- Memory: below 100 MB target for MVP.
- Disk: bounded spool with configurable max size.
- Network: compressed batched uploads.
- Communication: outbound only by default.
- Reliability: at least 24 hours of local queue by default, configurable.

Backend:

- Handle at least 1,000 agents in early load tests.
- Architecture should scale to 10,000 agents per controller cluster after tuning.
- Batch ingestion must be idempotent.
- UI list endpoints must paginate and filter.

Security:

- No plaintext persisted secrets.
- TLS verification enabled by default.
- Enrollment token shown once.
- RBAC and audit for all fleet operations.
- Signed config and signed updates before broad release.
