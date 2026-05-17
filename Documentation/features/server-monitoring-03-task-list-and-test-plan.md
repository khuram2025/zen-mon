# Server Monitoring Task List And Test Plan

Date: 2026-05-15

## Delivery Principles

- Ship the complete Windows agent monitoring loop before expanding broadly.
- Keep host agents and remote sensors distinct.
- Reuse existing sensor enrollment/config/result patterns where they fit, but create separate agent APIs because per-host agents have different lifecycle, security, and telemetry needs.
- Put durable queueing, secure enrollment, and fleet health into the first production design.
- Keep UI workflows practical for administrators: install, verify, diagnose, update, and decommission.

## Epic 1: Product Contracts

- [ ] Define `server` entity contract.
- [ ] Define `agent` entity contract.
- [ ] Define collection modes: Windows agent, Windows agentless, SNMP, Linux agent, Linux SSH.
- [ ] Define agent policy schema.
- [ ] Define metric naming conventions.
- [ ] Define tag model and standard tags: site, environment, owner, OS, role.
- [ ] Define host status state machine: healthy, warning, critical, unknown, stale, disabled.
- [ ] Define agent status state machine: enrolling, online, stale, offline, disabled, updating, error.
- [ ] Define server-to-device relationship.
- [ ] Define retention policy for raw metrics and rollups.
- [ ] Define first alert templates and default thresholds.
- [ ] Define RBAC permissions for server monitoring and agent fleet actions.
- [ ] Define audit log event names.

## Epic 2: Database And Migrations

- [ ] Add Postgres migration for `servers`.
- [ ] Add Postgres migration for `server_tags`.
- [ ] Add Postgres migration for `agents`.
- [ ] Add Postgres migration for `agent_policies`.
- [ ] Add Postgres migration for `agent_policy_assignments`.
- [ ] Add Postgres migration for `agent_enrollment_tokens`.
- [ ] Add Postgres migration for `agent_packages`.
- [ ] Add Postgres migration for `agent_commands`.
- [ ] Add Postgres migration for `agent_command_results`.
- [ ] Add Postgres migration for `agent_diagnostics`.
- [ ] Add Postgres migration for `server_service_inventory`.
- [ ] Add Postgres migration for `server_process_inventory`.
- [ ] Add Postgres migration for `server_filesystem_inventory`.
- [ ] Add Postgres migration for `server_network_interface_inventory`.
- [ ] Add Postgres migration for `server_software_inventory`.
- [ ] Add ClickHouse migration for `host_cpu_metrics`.
- [ ] Add ClickHouse migration for `host_memory_metrics`.
- [ ] Add ClickHouse migration for `host_filesystem_metrics`.
- [ ] Add ClickHouse migration for `host_disk_io_metrics`.
- [ ] Add ClickHouse migration for `host_network_metrics`.
- [ ] Add ClickHouse migration for `host_process_metrics`.
- [ ] Add ClickHouse migration for `host_service_state`.
- [ ] Add ClickHouse migration for `host_event_log_summary`.
- [ ] Add ClickHouse migration for `agent_health_metrics`.
- [ ] Add ClickHouse rollup materialized views.
- [ ] Add database indexes for server list filtering.
- [ ] Add uniqueness constraints for `agent_uid`, host identity, and package version/platform.

## Epic 3: Backend Agent Runtime APIs

- [ ] Create `server/app/api/v1/agents.py`.
- [ ] Add `POST /api/v1/agents/enroll`.
- [ ] Add enrollment token creation, hashing, expiry, and one-time use.
- [ ] Add host identity reconciliation during enrollment.
- [ ] Add agent credential issuance.
- [ ] Add `POST /api/v1/agents/heartbeat`.
- [ ] Add heartbeat status update and stale detection.
- [ ] Add `GET /api/v1/agents/config` with ETag support.
- [ ] Add signed config payload generation.
- [ ] Add `POST /api/v1/agents/results/host`.
- [ ] Add idempotent batch acceptance.
- [ ] Add validation for metric envelope schema.
- [ ] Add partial batch rejection reporting.
- [ ] Add backpressure response hints.
- [ ] Add `POST /api/v1/agents/events`.
- [ ] Add `POST /api/v1/agents/diagnostics`.
- [ ] Add `GET /api/v1/agents/packages/manifest`.
- [ ] Add package download endpoint.
- [ ] Add command polling endpoints for safe commands.
- [ ] Add audit log writes for enrollment, config changes, token creation, package download, command issue, and command result.
- [ ] Add RBAC checks for all admin operations.

## Epic 4: Backend Admin APIs

- [ ] Add `GET /api/v1/servers` with pagination, sorting, and filters.
- [ ] Add `POST /api/v1/servers`.
- [ ] Add `GET /api/v1/servers/{id}`.
- [ ] Add `PATCH /api/v1/servers/{id}`.
- [ ] Add `GET /api/v1/servers/{id}/metrics`.
- [ ] Add `GET /api/v1/servers/{id}/processes`.
- [ ] Add `GET /api/v1/servers/{id}/services`.
- [ ] Add `GET /api/v1/servers/{id}/events`.
- [ ] Add `GET /api/v1/servers/{id}/agent`.
- [ ] Add `POST /api/v1/servers/{id}/install-token`.
- [ ] Add `POST /api/v1/servers/{id}/decommission`.
- [ ] Add `GET /api/v1/agent-policies`.
- [ ] Add `POST /api/v1/agent-policies`.
- [ ] Add `PATCH /api/v1/agent-policies/{id}`.
- [ ] Add `GET /api/v1/agent-fleet`.
- [ ] Add fleet bulk update endpoints.
- [ ] Add stale server background job.
- [ ] Add package manifest admin upload/register flow.

## Epic 5: ClickHouse Ingestion And Rollups

- [ ] Implement host metric insert service.
- [ ] Implement inventory upsert service.
- [ ] Implement service state insert service.
- [ ] Implement event summary insert service.
- [ ] Implement agent health insert service.
- [ ] Add rollup queries for UI charts.
- [ ] Add retention settings.
- [ ] Add high-cardinality guardrails for process names, labels, and paths.
- [ ] Add query helpers for top CPU, top memory, low disk, and stale agents.
- [ ] Add ingestion metrics and error counters for backend observability.

## Epic 6: Windows Agent Runtime

- [ ] Create a new Go module/package for `zenplus-agent.exe`.
- [ ] Implement Windows service install/start/stop hooks.
- [ ] Implement bootstrap config reader.
- [ ] Implement enrollment client.
- [ ] Implement secure credential storage abstraction using Windows DPAPI.
- [ ] Implement heartbeat loop.
- [ ] Implement config polling with ETag.
- [ ] Implement config signature verification.
- [ ] Implement scheduler with jitter.
- [ ] Implement bounded worker pool.
- [ ] Implement durable spool.
- [ ] Implement batch uploader with retry and exponential backoff.
- [ ] Implement controller backpressure handling.
- [ ] Implement CPU collector.
- [ ] Implement memory collector.
- [ ] Implement filesystem collector.
- [ ] Implement disk IO collector.
- [ ] Implement network collector.
- [ ] Implement OS and hardware inventory collector.
- [ ] Implement process summary collector.
- [ ] Implement process watchlist collector.
- [ ] Implement Windows service collector.
- [ ] Implement Windows Event Log summary collector.
- [ ] Implement agent self-health collector.
- [ ] Implement local log file with rotation.
- [ ] Implement Windows Event Log source for agent errors.
- [ ] Implement proxy support.
- [ ] Implement private CA support.
- [ ] Implement graceful shutdown and restart handling.
- [ ] Implement local diagnostic command.
- [ ] Implement support bundle generation with redaction.

## Epic 7: Windows MSI Packaging

- [ ] Create WiX Toolset project.
- [ ] Package `zenplus-agent.exe`.
- [ ] Add service install action for `ZenPlus Agent`.
- [ ] Add service stop/start/remove actions.
- [ ] Add per-machine install under `%ProgramFiles%\ZenPlus\Agent`.
- [ ] Add ProgramData state/config/log directories.
- [ ] Add restricted ACLs.
- [ ] Add MSI properties: `CONTROLLER_URL`, `ENROLLMENT_TOKEN`, `AGENT_NAME`, `SITE_ID`, `POLICY_ID`, `PROXY_URL`, `VERIFY_TLS`, `INSTALLDIR`.
- [ ] Add silent install support.
- [ ] Add GPO/SCCM/Intune compatible install command.
- [ ] Add upgrade code and MajorUpgrade behavior.
- [ ] Add uninstall behavior that preserves state by default.
- [ ] Add purge option for uninstall.
- [ ] Add Authenticode signing step.
- [ ] Add SHA256 checksum generation.
- [ ] Add package manifest generation.
- [ ] Add package upload/register process.
- [ ] Add Windows CI runner build.
- [ ] Add installer smoke tests.

## Epic 8: Dashboard UI

- [ ] Add `ServersPage`.
- [ ] Add server overview KPIs.
- [ ] Add server table with filters, sorting, pagination, and saved views.
- [ ] Add Add Server flow.
- [ ] Add Windows Agent install step with MSI download and command copy.
- [ ] Add Agentless Windows setup step.
- [ ] Add server detail overview tab.
- [ ] Add CPU/memory/disk/network chart components.
- [ ] Add filesystems tab.
- [ ] Add network tab.
- [ ] Add processes tab.
- [ ] Add services tab.
- [ ] Add events tab.
- [ ] Add inventory tab.
- [ ] Add agent tab.
- [ ] Add alerts tab.
- [ ] Add timeline tab.
- [ ] Add `AgentFleetPage`.
- [ ] Add policy builder UI.
- [ ] Add bulk actions for fleet operations.
- [ ] Add stale/offline/error empty states.
- [ ] Add loading skeletons and error recovery states.
- [ ] Add responsive layouts for desktop and tablet.
- [ ] Add accessibility labels and keyboard navigation for core actions.

## Epic 9: Alerts And Reporting

- [ ] Add server stale alert template.
- [ ] Add high CPU alert template.
- [ ] Add high memory alert template.
- [ ] Add low disk alert template.
- [ ] Add disk IO saturation alert template.
- [ ] Add network errors alert template.
- [ ] Add Windows service stopped alert template.
- [ ] Add Event Log critical spike alert template.
- [ ] Add agent queue growing alert template.
- [ ] Add agent config apply failed alert template.
- [ ] Add agent update failed alert template.
- [ ] Add server availability report.
- [ ] Add capacity report for CPU, memory, disk growth.
- [ ] Add agent fleet health report.

## Epic 10: Security And Compliance

- [ ] Add threat model for Windows agent and agentless monitoring.
- [ ] Add enrollment token one-time use tests.
- [ ] Add token expiry and scope enforcement.
- [ ] Add signed config key management.
- [ ] Add signed update manifest verification.
- [ ] Add mTLS design and certificate lifecycle.
- [ ] Add certificate rotation endpoint.
- [ ] Add RBAC permissions.
- [ ] Add audit log coverage.
- [ ] Add credential vault design for agentless WMI/WinRM/SSH.
- [ ] Add secret redaction in logs and diagnostics.
- [ ] Add package checksum and signature display in UI.
- [ ] Add secure uninstall/decommission behavior.

## Epic 11: Agentless Windows

- [ ] Extend remote sensor config contract for WMI/WinRM checks.
- [ ] Add Windows credential model and assignment UI.
- [ ] Add sensor-side WMI collector.
- [ ] Add sensor-side WinRM/PowerShell collector.
- [ ] Add sensor-side SNMP fallback for servers.
- [ ] Add per-target timeout and concurrency limits.
- [ ] Add credential validation flow.
- [ ] Add agentless result ingestion path.
- [ ] Add UI status for credential, permission, and firewall failures.
- [ ] Add documentation for Windows firewall and permissions.

## Epic 12: Linux Agent And SSH

- [ ] Implement Linux service using systemd.
- [ ] Add `.deb` package.
- [ ] Add `.rpm` package.
- [ ] Add Linux collectors for CPU, memory, load, filesystem, disk IO, network.
- [ ] Add systemd service collector.
- [ ] Add process summary collector.
- [ ] Add journal summary collector.
- [ ] Add package inventory collector.
- [ ] Add Linux agent install UI.
- [ ] Add SSH-based agentless checks through remote sensor.

## Epic 13: Application Monitoring Extension

- [ ] Add process-to-service discovery.
- [ ] Add listening port discovery.
- [ ] Add IIS template.
- [ ] Add SQL Server template.
- [ ] Add Active Directory template.
- [ ] Add DNS/DHCP template.
- [ ] Add Hyper-V template.
- [ ] Add NGINX template.
- [ ] Add PostgreSQL template.
- [ ] Add MySQL template.
- [ ] Add Redis template.
- [ ] Add Docker/container host template.
- [ ] Add log ingestion design.
- [ ] Add OpenTelemetry Collector compatibility design.
- [ ] Add application entity model.
- [ ] Add service dependency map design.

## Backend Test Plan

Unit tests:

- Enrollment token hashing, expiry, scope, and one-time use.
- Agent identity reconciliation.
- Heartbeat state transition.
- Config ETag behavior.
- Config signature generation.
- Metric envelope validation.
- Batch idempotency.
- Partial batch rejection.
- Backpressure response.
- Package manifest validation.
- RBAC permission checks.
- Audit event creation.

Integration tests:

- Enroll agent and create server record.
- Enroll with invalid token fails.
- Enroll with expired token fails.
- Enroll with reused token fails.
- Heartbeat updates agent and server status.
- Config endpoint returns 304 for unchanged ETag.
- Host metrics insert into ClickHouse.
- Inventory upsert into Postgres.
- Stale agent background job changes state.
- Package download requires valid role.
- Command polling returns only allowed commands.
- Diagnostic upload stores metadata and redacts secrets.

Migration tests:

- Postgres migrations apply cleanly from empty database.
- ClickHouse migrations apply cleanly.
- Rollup views populate expected aggregates.
- Rollback or forward-fix plan is documented for every migration.

## Agent Test Plan

Unit tests:

- Bootstrap config parsing.
- Enrollment client request/response handling.
- DPAPI abstraction with test double.
- Config signature verification.
- Scheduler interval and jitter behavior.
- Bounded worker pool behavior.
- Spool write/read/ack/retry behavior.
- Batch uploader retry and backoff.
- Backpressure handling.
- Collector timeout behavior.
- Redaction logic.

Windows integration tests:

- MSI installs silently.
- Service starts after install.
- Service restarts after reboot.
- Service stops and uninstalls cleanly.
- Upgrade preserves identity.
- Failed upgrade rolls back.
- Uninstall preserves state by default.
- `PURGE=1` removes state.
- Agent enrolls through HTTPS.
- Agent supports proxy configuration.
- Agent rejects invalid TLS by default.
- Agent uses private CA when configured.
- Network outage queues data.
- Queue drains after network recovery.
- Service runs with restricted permissions.
- Windows Event Log collector reads expected event counts.
- Performance counter collection works on Windows Server 2019, 2022, and 2025 test images where available.

Performance tests:

- CPU below target during baseline collection.
- Memory below target during baseline collection.
- Disk spool remains bounded under controller outage.
- Upload compression reduces network usage.
- High process count does not create unbounded payloads.
- Event log burst does not block metric collection.

Security tests:

- Bootstrap token is removed or made unusable after enrollment.
- Stored credential is not plaintext.
- Agent refuses unsigned config.
- Agent refuses config signed by wrong key.
- Agent refuses unsigned update manifest.
- Agent refuses tampered package checksum.
- Diagnostics bundle redacts secrets.

## UI Test Plan

Unit/component tests:

- Server overview KPI rendering.
- Server table filters and sorting.
- Add Server mode selection.
- MSI command rendering.
- Token hidden after modal close.
- Server detail chart empty states.
- Services tab state badges.
- Agent tab status and action states.
- Fleet table bulk selection.
- Policy builder form validation.

Integration tests:

- Create install token from Add Server flow.
- Download package link is visible when package exists.
- Server appears after mock enrollment.
- Stale server status appears after heartbeat timeout.
- Agent diagnostics action shows pending/completed states.
- Policy change updates config hash.
- Alert template creation from server detail.

Accessibility tests:

- Keyboard navigation through Add Server flow.
- Dialog focus trapping.
- Button labels for icon actions.
- Color contrast for health states.
- Screen reader labels for chart summaries.

Build tests:

- `npm run build`.
- TypeScript compile.
- Lint if configured.

## End-To-End Test Plan

Scenarios:

- Admin creates Windows agent install token, installs MSI, agent enrolls, metrics appear, alert fires, alert clears.
- Agent is offline for 30 minutes, queues data, reconnects, uploads data, UI backfills charts.
- Agent upgrades from version N to N+1 and remains healthy.
- Agent receives bad config, rejects it, keeps last known good config, reports error.
- Agent certificate rotates successfully.
- Decommissioned agent cannot upload data.
- Agentless Windows check discovers host metrics through remote sensor.
- RBAC viewer can see server metrics but cannot rotate certificates or issue diagnostics.

## Load Test Plan

Targets:

- 1,000 agents for MVP validation.
- 10,000 agents for architecture validation.

Tests:

- Heartbeat storm after controller restart.
- Metric batch ingestion at expected intervals.
- Package manifest checks from large fleet.
- Config ETag polling from large fleet.
- Server list UI query with 10,000 records.
- Server detail query over 30 days of rollups.
- ClickHouse insert throughput and merge pressure.
- Postgres connection pool behavior under fleet operations.

## First Four Sprints

### Sprint 1: Contracts And Backend Skeleton

- [ ] Finalize API contracts.
- [ ] Add migrations.
- [ ] Add agent enroll/heartbeat/config skeleton.
- [ ] Add package manifest endpoint.
- [ ] Add basic server list API.
- [ ] Add test fixtures.

### Sprint 2: Windows Agent Prototype And MSI

- [ ] Build Windows service prototype.
- [ ] Implement enroll/heartbeat/config.
- [ ] Implement CPU/memory/filesystem/network collectors.
- [ ] Implement durable spool.
- [ ] Create WiX MSI.
- [ ] Run install/uninstall smoke tests.

### Sprint 3: Ingestion And UI MVP

- [ ] Implement host metric ingestion.
- [ ] Add ClickHouse queries.
- [ ] Build Server Overview page.
- [ ] Build Server Detail overview.
- [ ] Build Add Server install flow.
- [ ] Add basic alert templates.

### Sprint 4: Reliability And Fleet Operations

- [ ] Add signed config verification.
- [ ] Add package signing/checksum display.
- [ ] Add agent diagnostics.
- [ ] Add Agent Fleet page.
- [ ] Add update ring fields.
- [ ] Add stale detection.
- [ ] Run E2E and load tests.

## Definition Of Done

The feature is not done until:

- Documentation, API contracts, and security model are current.
- Backend tests cover enrollment, heartbeat, config, results, package manifest, and RBAC.
- Agent tests cover install, service lifecycle, collection, queueing, upload, and security checks.
- UI tests cover Add Server, server overview, server detail, and agent fleet.
- Load tests prove the selected MVP scale.
- Installer is signed or signing process is documented with a temporary internal certificate for pre-production.
- Operational runbooks exist for install, upgrade, uninstall, diagnostics, and controller outage recovery.
