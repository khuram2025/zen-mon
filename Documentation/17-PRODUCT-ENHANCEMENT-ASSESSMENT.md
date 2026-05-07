# ZenPlus Product Enhancement Assessment

Date: 2026-05-06

## Executive Summary

ZenPlus already has a credible foundation for a professional infrastructure monitoring platform. The current system includes a Go poller, FastAPI control plane, React dashboard, ClickHouse time-series storage, PostgreSQL configuration storage, Redis real-time events, SNMP polling and traps, service checks, alerting, reports, OTA updates, and an early remote-sensor architecture.

Compared with market leaders, ZenPlus is currently strongest as a focused network and infrastructure monitor. It is not yet a full enterprise observability or AIOps platform. The main gaps are production-grade remote agents, topology and dependency mapping, cloud and Kubernetes monitoring, log/traces/APM support, mature alert correlation, enterprise security, automated test coverage, and an LLM/agent architecture built on reliable telemetry.

## Current Strengths

- Multi-service architecture: FastAPI API, Go poller, React dashboard, ClickHouse, PostgreSQL, Redis.
- Device inventory with grouping, type, location, tags, ping settings, and SNMP settings.
- ICMP monitoring with up/down/degraded state.
- Service checks for HTTP, TCP, TLS, ICMP, and DNS.
- SNMP v1/v2c/v3 foundation, encrypted SNMPv3 passphrases, profile support, discovery, MIB upload, interface metrics, entity inventory, environmental sensors, and traps.
- ClickHouse retention and rollup design for ping and service metrics.
- Alerts, alert rules, acknowledgement, resolution, templates, and notification channels.
- Maintenance windows and parent-check suppression for service checks.
- Dashboard pages for devices, services, alerts, reports, discovery, SNMP profiles, users, settings, gateways, and subscription.
- Report generation for executive, technical, business, and inventory views.
- OTA update subsystem with signed releases, health checks, rollback steps, inventory, and local update history.
- Early distributed sensor model with sites, sensors, enrollment, heartbeat, config pull, assignments, and result ingestion.

## Important Internal Gaps

### 1. Integration Correctness

Some UI/API paths and helpers are inconsistent. Example: the sidebar linked to `/service-checks` while the router defines `/services`. The report data helper for device status logs had a recursion bug that hid status-log data.

### 2. Poller Scheduling

The database stores `ping_interval` and `check_interval`, but the poller previously ran all enabled devices and service checks every 60 seconds. Professional monitoring requires interval-aware scheduling, jitter control, and load-aware execution.

### 3. Remote Sensors

The current remote-sensor API design is promising, but the shipped sensor is still Phase 1/mock-oriented. A production system needs a real sensor binary/service with local queueing, retry, signed config, versioning, self-health, and secure upgrades.

### 4. Enterprise Security

JWT authentication exists, but the enterprise layer needs route-level RBAC enforcement, audit logs, SSO/OIDC/SAML, MFA, API token lifecycle management, tenant isolation, secret rotation, and compliance-ready logs.

### 5. Test Coverage

There is no visible comprehensive automated test suite for backend, poller, dashboard, migrations, or integration flows. Monitoring products must have strong regression coverage because failures can silently break alerting and reporting.

### 6. Observability Scope

ZenPlus does not yet cover logs, traces, APM, OpenTelemetry ingestion, Kubernetes, cloud platforms, WMI/WinRM, NetFlow/sFlow/IPFIX, or synthetic browser checks.

## Market Leader Benchmark

The direct infrastructure and network monitoring benchmark is LogicMonitor, PRTG, SolarWinds NPM, and Zabbix. The higher-end observability and AI benchmark is Datadog, Dynatrace, and New Relic.

Market leaders commonly provide:

- Agentless and agent-based collection.
- Remote collectors, probes, or proxies for branch sites.
- Automatic discovery and continuous inventory.
- SNMP, WMI, SSH, API, NetFlow, sFlow, jFlow, and IPFIX support.
- Network topology maps and dependency maps.
- Dynamic thresholds, anomaly detection, alert deduplication, and event correlation.
- Metrics, logs, traces, APM, synthetic monitoring, RUM, Kubernetes, and cloud integrations.
- Mature dashboards, reports, mobile access, notification escalation, and on-call workflows.
- Enterprise RBAC, SSO, audit logs, API keys, and compliance posture.
- Plugin and integration ecosystems.
- AI-assisted root cause analysis and remediation workflows.

## Competitive Gap Matrix

| Capability | ZenPlus Today | Market Leader Expectation | Gap |
|---|---:|---:|---|
| ICMP availability | Strong | Standard | Low |
| HTTP/TCP/TLS/DNS checks | Good | Standard plus synthetic journeys | Medium |
| SNMP polling/traps | Good foundation | Deep templates, discovery, topology | Medium |
| Remote probes/sensors | Early/mock | Production collectors with buffering and upgrades | High |
| Dashboarding | Good foundation | Highly customizable dashboards and maps | Medium |
| Reporting | Good foundation | Scheduled, branded, SLA, compliance reports | Medium |
| Alerting | Basic to moderate | Correlated, deduped, escalated, anomaly-aware | High |
| Topology/dependencies | Minimal | LLDP/CDP/cloud/service dependency maps | High |
| Logs | Missing | Central log ingestion/search/correlation | High |
| APM/traces | Missing | OpenTelemetry/APM tracing | High |
| Cloud/Kubernetes | Missing | Native AWS/Azure/GCP/Kubernetes | High |
| Host monitoring | Missing dedicated agent | Linux/Windows agents and WMI/SSH | High |
| Enterprise security | Partial | SSO, MFA, RBAC, audit, tenant isolation | High |
| AI/LLM operations | Missing | RCA, explanation, remediation, incident assistant | High |
| Automated tests | Weak/unclear | CI-covered backend, poller, UI, migrations | High |

## Target Architecture

### Collection Layer

Use central pollers plus production remote sensors. Collectors should be plugin-based:

- ICMP, SNMP, HTTP, TCP, TLS, DNS.
- SSH and WMI/WinRM.
- NetFlow, sFlow, jFlow, IPFIX.
- Cloud APIs for AWS, Azure, and GCP.
- Kubernetes and container collectors.
- OpenTelemetry metrics, logs, and traces.

### Data Layer

Keep ClickHouse as the telemetry store. Add normalized schemas for events, logs, topology edges, incidents, and AI evidence. Enforce retention tiers, rollups, cardinality controls, and tenant partitioning.

### Topology Layer

Build relationship data from LLDP/CDP, routing neighbors, switch MAC/ARP tables, cloud metadata, Kubernetes ownership, service dependencies, and manual overrides.

### Alert Intelligence Layer

Move from threshold-only alerting to event intelligence:

- Alert grouping and deduplication.
- Dependency-based suppression.
- Maintenance-aware suppression.
- Dynamic baselines and anomaly detection.
- Forecasting and capacity warnings.
- Incident lifecycle and escalation policies.

### LLM/Agent Layer

The LLM should not directly mutate infrastructure at first. It should use controlled tools:

- Explain alert.
- Summarize device/service history.
- Correlate metrics, logs, events, and topology.
- Suggest root cause.
- Generate remediation plan.
- Run approved diagnostics through a safe command gateway.
- Create or update incident records.
- Recommend alert tuning.

### Enterprise Layer

Add SSO/OIDC/SAML, MFA, full RBAC, audit logs, encrypted secrets, API-token lifecycle, tenant isolation, HA deployment, backup/restore, upgrade validation, and compliance reports.

## Phase 1 Stabilization Plan

Phase 1 should stabilize the existing product before adding major new domains.

1. Fix known correctness issues:
   - Dashboard route mismatch.
   - Report status-log helper recursion.
   - Poller interval scheduling.

2. Add automated verification:
   - Go unit tests for scheduler behavior and status transitions.
   - Backend tests for auth, device/service APIs, reports, and alert rules.
   - Dashboard build checks and route smoke tests.
   - Migration smoke tests against clean databases.

3. Harden security:
   - Route-level role checks.
   - Admin-only protection for users, settings, updates, and credentials.
   - Audit trail table and writer.
   - Strong production defaults for JWT/CORS/secrets.

4. Normalize migrations:
   - Establish one migration runner.
   - Track applied migrations.
   - Add idempotent validation checks.

5. Prepare production remote sensors:
   - Replace mock sensor with a real service.
   - Add local queue, retry, backoff, config versioning, and self-health.
   - Add signed sensor config and binary upgrade path.

## Recommended Roadmap

### Phase 1: Stabilize Existing Product

- Fix correctness defects.
- Honor configured polling intervals.
- Add automated tests and CI checks.
- Harden auth, RBAC, CORS, and secret handling.
- Normalize migrations.

### Phase 2: Professional Network Monitoring

- Production remote sensor.
- LLDP/CDP topology.
- NetFlow/sFlow/IPFIX. NetFlow v5 collection, storage, API, and dashboard have now been implemented as the first flow-monitoring slice; NetFlow v9/IPFIX/sFlow decoders and enrichment remain next.
- SNMP vendor templates.
- Alert deduplication and escalation.
- Interface capacity dashboards.

### Phase 3: Enterprise Infrastructure Monitoring

- Linux and Windows host agents.
- WMI/WinRM/SSH collectors.
- VMware/Hyper-V.
- AWS/Azure/GCP.
- Kubernetes and container monitoring.
- SSO, MFA, audit logs, and tenant isolation.

### Phase 4: Observability and AI

- Logs and OpenTelemetry traces.
- Incident timeline and correlation engine.
- Anomaly detection and forecasting.
- LLM assistant with tool calling.
- Agentic remediation workflows with approval gates.

## Strategic Recommendation

Do not start with LLM agents as the main feature. First make telemetry, scheduling, topology, alerting, security, and tests reliable. Once the monitoring substrate is trustworthy, the LLM layer can deliver real value: evidence-backed root cause analysis, guided remediation, incident summaries, and operator assistance.
