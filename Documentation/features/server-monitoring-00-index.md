# Server Monitoring Documentation Index

Date: 2026-05-15

This documentation set covers the planned server monitoring feature for Windows and Linux, including agent and agentless monitoring, competitor research, implementation roadmap, task breakdown, test plan, and the first Windows MSI agent design.

## Documents

- [Market And Current System Assessment](server-monitoring-01-market-and-current-system-assessment.md)
  - Current ZenPlus Monitor capabilities.
  - Current gaps.
  - Competitor research across SolarWinds, Datadog, Dynatrace, New Relic, Zabbix, PRTG, ManageEngine, LogicMonitor, Site24x7, Checkmk, Nagios, Prometheus, Elastic, and OpenTelemetry.
  - Recommended hybrid agent plus agentless strategy.

- [Feature Plan And Roadmap](server-monitoring-02-feature-plan-and-roadmap.md)
  - Product model for servers, agents, policies, and remote sensors.
  - Backend, UI, security, and ClickHouse architecture.
  - Phased roadmap from Windows agent MVP to application-level monitoring and correlation.
  - MVP acceptance criteria and non-functional targets.

- [Task List And Test Plan](server-monitoring-03-task-list-and-test-plan.md)
  - Engineering epics with implementation checklists.
  - Backend, agent, Windows integration, UI, E2E, load, and security test plans.
  - First four sprint breakdown.
  - Definition of done.

- [Windows Agent And MSI Design](server-monitoring-04-windows-agent-msi-design.md)
  - Windows agent runtime architecture.
  - MSI packaging, service installation, filesystem layout, enrollment, security, updates, diagnostics, and compatibility matrix.
  - Metric collectors and batch upload contract.

## Main Product Direction

Build the Windows agent first, keep the existing remote sensor as the foundation for agentless WMI/WinRM/SNMP checks, and design the host telemetry pipeline so Linux and application-level monitoring can reuse the same entity, policy, ingestion, alerting, and UI model.
