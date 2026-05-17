# Server Monitoring: Market And Current System Assessment

Date: 2026-05-15

## Objective

Build first-class server monitoring for Windows and Linux with both agent and agentless collection. The first implementation target is a reliable, secure, performance-efficient Windows agent delivered as an MSI. The product should also keep the current remote sensor model because it is the right foundation for agentless monitoring and distributed polling.

This assessment covers:

- What the current ZenPlus Monitor project already has.
- What famous competitors provide.
- What gaps must be closed.
- The recommended product direction before implementation starts.

## Executive Recommendation

Use a hybrid model:

- Windows agent first for deep, reliable host telemetry.
- Remote sensor based agentless monitoring for WMI, WinRM, SNMP, and later SSH.
- Linux agent second, after the Windows agent contract and ingestion path are stable.
- Application-level monitoring as an extension of the same agent pipeline, not a separate product.

Agentless monitoring is important for environments where agents cannot be installed, but it is not enough for modern server monitoring. It has credential, firewall, scale, latency, and reliability limits, especially with Windows WMI/WinRM. The default user flow should present the agent as recommended and agentless as an alternative.

## Current Project Assessment

### Existing Strengths

The project already has several building blocks that should be reused.

| Area | Current capability | Relevant files |
| --- | --- | --- |
| Remote sites and sensors | Site and sensor admin APIs, sensor token issue/rotate/disable, assignment APIs, appliance downloads, configured OVA/ISO generation | `server/app/api/v1/sensors.py`, `server/app/schemas/sensor.py`, `scripts/migrate-008-sensors.sql` |
| Sensor runtime | Go sensor with enrollment, heartbeat, ETag config polling, ICMP checks, HTTP/TCP/TLS/DNS service checks, batching | `poller/cmd/sensor/main.go`, `poller/internal/checker/*` |
| Runtime sensor API | Sensor enrollment, heartbeat, config, results, events, binary and install downloads | `server/app/api/v1/sensor_api.py` |
| Device inventory | Device model supports type `server`, ping, SNMP, status, last seen, vendor, model, OS version | `server/app/models/device.py`, `server/app/api/v1/devices.py` |
| SNMP telemetry | SNMP current/detail APIs, ClickHouse metrics, interface metrics, traps, rollups | `scripts/migrate-004-snmp-clickhouse.sql`, `server/app/api/v1/devices.py` |
| Dashboard foundations | Remote sensor card, download/token dialogs, device list, SNMP CPU/memory display | `dashboard/src/components/SensorsCard.tsx`, `dashboard/src/pages/DevicesPage.tsx` |
| Appliance docs | Remote sensor appliance design and install process | `Documentation/19-PRODUCTION-REMOTE-SENSORS.md`, `Documentation/20-LIGHTWEIGHT-SENSOR-ARCHITECTURE.md`, `sensor-appliance/README.md` |

### Current Gaps

| Gap | Why it matters |
| --- | --- |
| No host/server entity model | Server monitoring needs a first-class host identity, OS, agent identity, site, tags, owner, lifecycle state, and relationship to device records. |
| No Windows/Linux agent | Current sensor is a site appliance/poller. It is not a per-host agent and does not collect local host metrics. |
| No agentless WMI/WinRM/SSH flow | Agentless server monitoring needs credential vaulting, sensor-side execution, result schemas, and UI workflows. |
| No host metrics schema | ClickHouse currently stores ping/SNMP metrics, but not normalized CPU, memory, disk, filesystem, network, process, service, package, event, and agent health metrics. |
| No durable agent spool | Remote sensor has in-memory batching. Host agents must survive network outages and controller downtime without losing important telemetry. |
| No signed config/update channel | Enterprise agent fleets need signed config, package integrity, version pinning, update rings, rollback, and audit trails. |
| No agent fleet operations UI | Need fleet status, version distribution, stale agents, queue depth, update ring, config hash, policy, and diagnostics. |
| No server detail experience | Current device detail is network/SNMP oriented. Server users expect live resource charts, top processes, service state, event logs, disk inventory, NICs, and agent diagnostics. |
| No app-level extension model | Future application monitoring needs process/service discovery, templates, logs, OpenTelemetry-compatible ingestion, and dependency mapping. |

## Competitor Landscape

### SolarWinds

SolarWinds Server & Application Monitor is strong in agentless Windows monitoring through WMI, optional agents, application templates, AppInsight-style deep app views, Orion platform dashboards, PerfStack-style metric correlation, dependencies, and broad network/server/application coverage.

Product lesson:

- Agentless WMI/SNMP is table stakes.
- Optional agents are expected for locked-down networks and deeper telemetry.
- Application templates and correlation views are major differentiators.

Source: https://documentation.solarwinds.com/en/success_center/sam/content/sam-agents-sw1927.htm

### Datadog

Datadog centers on a single host agent with integrations, logs, APM, profiling, security telemetry, remote configuration, tags, dashboards, monitors, and large-scale fleet operations.

Product lesson:

- One agent should be extensible through integrations.
- Tagging and service ownership must be first-class.
- Remote config and fleet health are not optional for enterprise adoption.

Sources:

- https://docs.datadoghq.com/agent/
- https://docs.datadoghq.com/agent/guide/setup_remote_config/

### Dynatrace

Dynatrace OneAgent is known for deep automatic discovery, process/application injection, topology mapping, ActiveGate for controlled network paths, and AI-assisted problem correlation.

Product lesson:

- Automatic entity discovery and service topology become premium features.
- A gateway/proxy component is valuable for private networks.
- Root-cause analysis requires clean entity relationships, not only metrics.

Sources:

- https://docs.dynatrace.com/docs/setup-and-configuration/dynatrace-oneagent
- https://docs.dynatrace.com/docs/ingest-from/dynatrace-activegate

### New Relic

New Relic Infrastructure Agent collects host metrics, inventory, events, and integrates with logs, APM, OpenTelemetry, NRQL querying, alerts, and entity relationships.

Product lesson:

- Host monitoring should feed a common observability entity model.
- Inventory and configuration changes are useful troubleshooting signals.
- OpenTelemetry compatibility helps future-proof application monitoring.

Source: https://docs.newrelic.com/docs/infrastructure/install-infrastructure-agent/get-started/introduction-infrastructure-agent/

### Zabbix

Zabbix provides agents, Agent 2, active/passive checks, proxies for distributed monitoring, templates, discovery, SNMP, IPMI, JMX, alerting, and long-proven open-source operations.

Product lesson:

- Active agent mode and proxies solve firewall and scale problems.
- Templates reduce setup time and should be built into the product.
- Local buffering at proxies/agents is required for reliability.

Sources:

- https://www.zabbix.com/documentation/current/en/manual/concepts/agent
- https://www.zabbix.com/documentation/current/en/manual/concepts/agent2
- https://www.zabbix.com/documentation/current/en/manual/distributed_monitoring/proxies

### PRTG

PRTG uses probes and sensors, with common Windows monitoring through WMI, SNMP, remote probes, and many ready-made sensor types. It is valued for fast setup and broad protocol coverage.

Product lesson:

- A "probe/sensor" mental model is easy for administrators.
- Sensor count and polling limits must be visible to users.
- WMI is useful but needs scale guidance and fallback options.

Sources:

- https://www.paessler.com/manuals/prtg/monitoring_via_wmi
- https://www.paessler.com/manuals/prtg/remote_probes_and_multiple_probes

### ManageEngine OpManager

ManageEngine OpManager combines server, network, virtualization, service, process, and Windows/Linux monitoring with WMI, SNMP, CLI, and dashboards.

Product lesson:

- Server monitoring buyers expect service/process monitoring and OS-specific templates.
- Network and server monitoring should share inventory and alerting.

Source: https://www.manageengine.com/network-monitoring/server-monitoring.html

### LogicMonitor

LogicMonitor uses collectors to run monitoring from inside customer networks. It emphasizes agentless collection, property discovery, dynamic thresholds, topology, and broad integrations.

Product lesson:

- A collector/sensor model is valuable for agentless enterprise monitoring.
- Dynamic properties and automatic discovery reduce manual setup.

Source: https://www.logicmonitor.com/support/collectors/collector-overview/about-the-logicmonitor-collector

### Site24x7

Site24x7 combines server agents, application monitoring, cloud monitoring, synthetic checks, and on-premise pollers for private networks.

Product lesson:

- Keep both per-host agents and on-prem pollers.
- Server monitoring should naturally expand into application and cloud visibility.

Sources:

- https://www.site24x7.com/server-monitoring.html
- https://www.site24x7.com/help/getting-started/on-premise-poller.html

### Checkmk

Checkmk provides efficient agents, agent bakery/package generation, distributed sites, service discovery, and strong host/service state modeling.

Product lesson:

- Agent package customization is a meaningful admin feature.
- Service discovery and check inventory reduce noise and setup time.

Sources:

- https://docs.checkmk.com/latest/en/agent_windows.html
- https://docs.checkmk.com/latest/en/agent_linux.html
- https://docs.checkmk.com/latest/en/distributed_monitoring.html

### Nagios NCPA

Nagios NCPA provides a cross-platform monitoring agent with active/passive options, API access, plugins, metrics, and service/process checks.

Product lesson:

- Plugin extensibility is important for long-tail monitoring needs.
- The agent should expose diagnostics locally without opening unsafe inbound access by default.

Source: https://www.nagios.org/ncpa/

### Prometheus And Elastic

Prometheus uses exporters such as node_exporter and windows_exporter for host metrics. Elastic Agent and system integrations collect host metrics and logs into a larger observability pipeline.

Product lesson:

- Host metrics should use conventional names and labels where practical.
- OpenTelemetry and exporter compatibility reduce lock-in concerns.

Sources:

- https://github.com/prometheus/node_exporter
- https://github.com/prometheus-community/windows_exporter
- https://www.elastic.co/docs/reference/integrations/system
- https://opentelemetry.io/docs/collector/

## Collection Options

### Agent Based

Agent based monitoring installs a local service on each host.

Best for:

- Deep Windows and Linux metrics.
- Process, service, event log, package, and local application discovery.
- Reliable collection during controller/network outages.
- Secure outbound-only communication.
- Future app-level monitoring.

Costs:

- Requires package distribution, upgrades, security reviews, and endpoint trust.
- Needs careful CPU, memory, disk, and network budget controls.

### Agentless

Agentless monitoring runs from a sensor/collector and uses protocols like WMI, WinRM, SNMP, SSH, IPMI, and vendor APIs.

Best for:

- Environments where installing agents is blocked.
- Initial discovery.
- Lightweight availability/resource checks.
- Network device and appliance monitoring.

Costs:

- Credential management is harder.
- Firewalls and remote Windows permissions are harder.
- WMI/WinRM can be slow or fragile at scale.
- Telemetry depth is limited compared with a local agent.

### Remote Sensor

The current remote sensor should become the local execution point for agentless checks and private-network discovery.

Recommended responsibilities:

- WMI/WinRM/SSH/SNMP checks.
- Discovery of nearby servers.
- Credential-bound checks without exposing credentials to the controller runtime.
- Local queueing for agentless results.
- Optional package cache for agent updates in restricted networks.

## Product Differentiators To Build

To compete with the famous vendors and eventually exceed them, build the following differentiators into the design from the start:

- Secure-by-default agent: outbound only, TLS verification on, token hash storage, DPAPI on Windows, signed config, signed updates.
- Hybrid topology: host agent, remote sensor, and agentless checks all use one entity model.
- Durable telemetry: local spool, backpressure handling, idempotent batch upload, resumable queue.
- Fleet operations: version distribution, update rings, config hash, stale status, queue depth, diagnostics bundle, remote safe commands.
- Best server UI: fleet overview, resource pressure, service/process/event context, server timeline, and clear agent health.
- Application-ready pipeline: process/service discovery now, OpenTelemetry-compatible app/log/trace path later.
- Opinionated templates: Windows baseline, IIS, SQL Server, Active Directory, DNS, DHCP, file server, Hyper-V, Linux baseline, NGINX, PostgreSQL, MySQL, Redis, Docker.
- Noise controls: anomaly detection later, but start with sane thresholds, maintenance windows, dependency suppression, and alert dedupe.
- Compliance and audit: who installed, rotated, updated, disabled, changed policy, or ran diagnostics.

## Recommended First Release Boundary

Do not try to deliver every competitor feature in the first release. The first release should prove the complete loop:

1. User creates a server monitoring policy.
2. User downloads a Windows MSI or install command.
3. Agent enrolls securely.
4. Agent sends heartbeat and host metrics.
5. Backend stores metrics and status.
6. UI shows server overview, server detail, agent fleet health, and alerts.
7. Agent survives restarts and network outages.
8. MSI supports install, upgrade, and uninstall.

Everything else should be layered after this loop is stable.

## Source Index

- SolarWinds SAM agents: https://documentation.solarwinds.com/en/success_center/sam/content/sam-agents-sw1927.htm
- Datadog Agent: https://docs.datadoghq.com/agent/
- Datadog remote configuration: https://docs.datadoghq.com/agent/guide/setup_remote_config/
- Dynatrace OneAgent: https://docs.dynatrace.com/docs/setup-and-configuration/dynatrace-oneagent
- Dynatrace ActiveGate: https://docs.dynatrace.com/docs/ingest-from/dynatrace-activegate
- New Relic infrastructure agent: https://docs.newrelic.com/docs/infrastructure/install-infrastructure-agent/get-started/introduction-infrastructure-agent/
- Zabbix agent: https://www.zabbix.com/documentation/current/en/manual/concepts/agent
- Zabbix Agent 2: https://www.zabbix.com/documentation/current/en/manual/concepts/agent2
- Zabbix proxies: https://www.zabbix.com/documentation/current/en/manual/distributed_monitoring/proxies
- PRTG WMI monitoring: https://www.paessler.com/manuals/prtg/monitoring_via_wmi
- PRTG remote probes: https://www.paessler.com/manuals/prtg/remote_probes_and_multiple_probes
- ManageEngine server monitoring: https://www.manageengine.com/network-monitoring/server-monitoring.html
- LogicMonitor Collector: https://www.logicmonitor.com/support/collectors/collector-overview/about-the-logicmonitor-collector
- Site24x7 server monitoring: https://www.site24x7.com/server-monitoring.html
- Site24x7 on-premise poller: https://www.site24x7.com/help/getting-started/on-premise-poller.html
- Checkmk Windows agent: https://docs.checkmk.com/latest/en/agent_windows.html
- Checkmk Linux agent: https://docs.checkmk.com/latest/en/agent_linux.html
- Checkmk distributed monitoring: https://docs.checkmk.com/latest/en/distributed_monitoring.html
- Nagios NCPA: https://www.nagios.org/ncpa/
- Prometheus node_exporter: https://github.com/prometheus/node_exporter
- Prometheus windows_exporter: https://github.com/prometheus-community/windows_exporter
- Elastic system integration: https://www.elastic.co/docs/reference/integrations/system
- OpenTelemetry Collector: https://opentelemetry.io/docs/collector/
- OpenTelemetry Collector deployment: https://opentelemetry.io/docs/collector/deployment/
- Microsoft WMI: https://learn.microsoft.com/en-us/windows/win32/wmisdk/wmi-start-page
- Microsoft Performance Counters: https://learn.microsoft.com/en-us/windows/win32/perfctrs/performance-counters-portal
- Microsoft Service Control Manager: https://learn.microsoft.com/en-us/windows/win32/services/service-control-manager
- Microsoft Windows Installer: https://learn.microsoft.com/en-us/windows/win32/msi/windows-installer-portal
- WiX Toolset: https://docs.firegiant.com/wix/
