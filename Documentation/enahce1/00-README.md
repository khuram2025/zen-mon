# enahce1 — ZenPlus Network Monitoring: Competitive Analysis & Enhancement Plan

This folder contains the deep, feature-level competitive analysis of the **ZenPlus network
monitoring module** against the major commercial and open-source players, plus the resulting
**prioritized enhancement roadmap with detailed implementation plans, task breakdowns, and test
cases**.

Baseline date: **2026-06-03**. Product baseline = live deployment `http://10.12.50.81/` +
codebase `/home/zen/zen-mon-push`.

## Document set

| File | Purpose |
|------|---------|
| `00-README.md` | This index. |
| `01-COMPETITIVE-ANALYSIS.md` | The full analysis: Executive Summary, Competitor Overview, Current Feature Analysis, Missing Feature Analysis, Improvement Opportunities, Gap-Filling Roadmap, Strategic Recommendations. |
| `02-FEATURE-COMPARISON-MATRIX.md` | Feature-by-feature scoring grid: ZenPlus vs. 15 competitors across 52 capabilities, with a table-stakes-gap signal. |
| `03-CURRENT-PRODUCT-INVENTORY.md` | Evidence-based "what we have today," module-by-module, mapped to the taxonomy. |
| `04-ROADMAP.md` | Prioritized roadmap: 15 epics, 4 phases, effort, business impact, dependency map, risks, KPIs. |
| `06-TEST-PLAN.md` | Consolidated test strategy and cross-epic test matrix. |

### Detailed implementation plans (`05-EPIC-*`)

Each epic doc has 12 sections: goal & rationale, scope, current state (with code paths), target design,
data model & migrations, API changes, poller changes, dashboard changes, **task breakdown table**,
acceptance criteria, **test-case table (≈18–20 cases)**, and risks & rollout.

| Epic | File | Phase |
|------|------|:----:|
| **E1** Multi-condition alert rules + dependency-aware suppression | `05-EPIC-E1-multi-condition-alerting.md` | 1 |
| **E2** SNMP trap → events pipeline & trap-based alerting | `05-EPIC-E2-snmp-trap-events.md` | 1 |
| **E3** Discovery → auto-topology + auto-import | `05-EPIC-E3-discovery-topology-autoimport.md` | 1 |
| **E4** Network Configuration Management (NCM) | `05-EPIC-E4-ncm-config-management.md` | 2 |
| **E5** RBAC + SSO/SAML/OIDC + multi-tenancy | `05-EPIC-E5-rbac-sso-multitenancy.md` | 2 |
| **E6** Distributed collectors + poller HA/failover | `05-EPIC-E6-distributed-collectors-ha.md` | 2 |
| **E7** Server/host agent fleet + app/DB checks | `05-EPIC-E7-server-agent-fleet.md` | 3 |
| **E8** AIOps — ML anomaly + correlation + forecast + GenAI copilot | `05-EPIC-E8-aiops-ml-genai-copilot.md` | 3 |
| **E9** On-call/escalation + incident mgmt + ITSM | `05-EPIC-E9-oncall-incident-itsm.md` | 3 |
| **E10** Custom dashboards / NOC video wall | `05-EPIC-E10-custom-dashboards-noc.md` | 3 |
| **E11** Synthetic + WAN/Internet path + BGP | `05-EPIC-E11-synthetic-wan-path-bgp.md` | 4 |
| **E12** NetFlow elevation — ML app-ID + forecasting + NDR-lite | `05-EPIC-E12-netflow-elevation-ndr.md` | 3 |

*(Roadmap epics E13–E15 — cloud/K8s, wireless, logs/SIEM/mobile/gNMI — are scoped in `04-ROADMAP.md`;
detailed plans to be authored when they enter a delivery phase.)*

## Method

1. **Our product**: live-app walkthrough (Devices, Device Detail, NetFlow + Anomaly/Capacity,
   Servers, Agent Fleet, Automated Maps, Discovery, Services, Alerts/Rules, Channels, Reports,
   Settings) + code evidence from `server/`, `poller/`, `dashboard/`.
2. **Competitors**: deep web research per vendor (official docs, pricing, reviews) scored against a
   common ~50-point capability taxonomy: SolarWinds, Datadog, Zabbix, Dynatrace, PRTG, LogicMonitor,
   Auvik, ManageEngine OpManager, Nagios/Icinga, Checkmk, LibreNMS/Observium, ThousandEyes, Kentik,
   Grafana stack, Domotz/NinjaOne.
3. **Synthesis**: gap analysis (critical / important / nice-to-have), improvement opportunities,
   and a prioritized roadmap with per-epic plans, tasks, and test cases.
