# ZenPlus — Gap-Filling Roadmap (Prioritized)

*Companion to `01-COMPETITIVE-ANALYSIS.md`. Per-epic plans with tasks + test cases live in `05-EPIC-*`.*

## Prioritization model

- **Impact** = revenue unlock (deal-blocker removal) + differentiation + breadth-of-buyer.
- **Effort** = T-shirt (S ≤2 wk · M ~3–6 wk · L ~7–12 wk · XL >12 wk) for a 2–3 engineer squad, including
  poller (Go), API (FastAPI), dashboard (React), and DB/migration work.
- **Sequence rule:** deal-blockers first → finish-the-80%-built → differentiators → coverage expansion.
  Respect dependencies (don't ship correlation before trap/events; don't sell MSP before multi-tenancy).

## Effort / impact / dependency table

| # | Epic | Cat | Effort | Impact | Depends on | Unlocks |
|---|------|-----|:------:|:------:|------------|---------|
| **E1** | Multi-condition alert rules + dependency-aware suppression | 🔴 | **S–M** (~3–4 wk) | **Very High** | alert_engine, Automated Maps links (E3 for full value) | credibility parity (15/15 have it); noise reduction |
| **E2** | SNMP trap → events & trap-based alerting | 🔴 | **S–M** (~3–4 wk) | High | poller `traps.go`, new events store | event-driven monitoring; parity (10/15) |
| **E3** | Discovery → auto-topology + auto-import (end-to-end) | 🔴 | **M** (~5–6 wk) | **Very High** | Discovery, Automated Maps, SNMP/LLDP | Auvik-class onboarding; powers E1 suppression |
| **E4** | NCM — backup/versioning/diff + change alerts (→ push/compliance) | 🔴 | **M–L** (phase 1 ~6 wk) | **Very High** | Credentials, device console, scheduler | biggest NetOps category gap; SW/ME/Auvik parity |
| **E5** | RBAC + SSO/SAML/OIDC + multi-tenancy | 🔴 | **M–L** (~7–9 wk) | **Very High** | `core/security.py`, users, tenant-scoped data | enterprise + MSP deals |
| **E6** | Distributed collectors + poller HA/failover | 🔴 | **L** (~9–12 wk) | **Very High** | poller, Redis, ClickHouse, config sync | scale, multi-site, resilience SLAs |
| **E7** | Server/host agent fleet maturation + app/DB checks | 🟠 | **M–L** (~8 wk) | High | Servers, Agent Fleet/Policies | server monitoring market; RMM-adjacent |
| **E8** | AIOps — ML baselining/anomaly + correlation + forecast + GenAI copilot | 🟠 | **L** (~10–12 wk) | **Very High** | ClickHouse metrics, alert engine, E1/E2 | 2026 differentiator; noise reduction |
| **E9** | On-call/escalation + incident mgmt + ITSM connectors | 🟠 | **M** (~5–6 wk) | High | Channels, alerts, service incidents | enterprise ops; Teams/Jira/ServiceNow ("Planned") |
| **E10** | Custom dashboards / NOC video wall | 🟠 | **M** (~5–6 wk) | Med-High | widget framework, metrics API | NOC buyers; demो polish |
| **E11** | Synthetic + WAN/Internet path (traceroute) + BGP basics | 🟠 | **M–L** (~8 wk) | Med-High | poller checkers, collectors/agents | digital-experience story vs ThousandEyes-lite |
| **E12** | NetFlow elevation — ML app-ID + forecasting + NDR-lite | 🟠 | **M** (~6 wk) | **High** | existing NetFlow + Anomaly Detection | defensible differentiator |
| **E13** | Cloud (AWS/Azure/GCP) + Kubernetes monitoring | 🟡 | **L** (~10 wk) | Med | collectors, agent, API integrations | hybrid buyers |
| **E14** | Wireless controller/AP + heatmaps | 🟡 | **M–L** (~8 wk) | Med | SNMP, maps, floor-plan UI | campus/retail/education buyers |
| **E15** | Logs/SIEM event console · mobile app · gNMI streaming | 🟡 | **L** (split) | Med | events store; mobile shell; poller | coverage completeness |

## Phase plan

### Phase 1 — Credibility & quick wins (0–3 months) · *"close the obvious gaps, finish the 80%-built"*
- **E1** Multi-condition alert rules + wire **alert→Channels routing** (Channels shows "Alerts: Needs channel").
- **E2** SNMP trap → events & trap-based alerts.
- **E3** Discovery → auto-topology + auto-import (populate Automated Maps; resolve unmapped neighbors).
- **Quick wins:** scheduled report delivery via Channels; make Health Score alertable; flapping/dedup basics.
- **Outcome:** every "single-condition / 0 links / needs channel / not populated" gap a prospect notices is closed.

### Phase 2 — Enterprise & MSP unlock (3–6 months) · *"become deal-viable above mid-market"*
- **E4** NCM phase 1 (backup/versioning/diff + change-detection alerts).
- **E5** RBAC + SSO/SAML/OIDC + multi-tenancy.
- **E6** Distributed collectors + poller HA/failover.
- **Outcome:** passes enterprise security/scale procurement; MSP multi-tenant motion becomes possible.

### Phase 3 — Differentiate (6–9 months) · *"win on noise-reduction, AI, and server depth"*
- **E8** AIOps: ML baselining/anomaly + correlation + capacity forecast + **GenAI copilot + MCP server**.
- **E12** NetFlow elevation (ML app-ID, forecasting, NDR-lite marketed module).
- **E7** Server/host agent maturation + app/DB checks.
- **E9** On-call/escalation + incident mgmt + ITSM (Teams/Jira/ServiceNow).
- **E10** Custom dashboards / NOC wall.
- **Outcome:** ZenPlus reads as a modern, AI-assisted, low-noise platform with a unique flow-security angle.

### Phase 4 — Coverage expansion (9–15 months) · *"breadth for whole-estate buyers"*
- **E11** Synthetic + WAN/Internet path + BGP · **E4 phase 2** (config push + compliance + firmware EoL/CVE).
- **E13** Cloud + Kubernetes · **E14** Wireless + heatmaps · **E15** Logs/event console, mobile app, gNMI.
- **Cross-cutting all phases:** REST API/IaC + **Terraform provider**, integrations breadth, transparent
  device-based pricing rollout, docs.

## Dependency map (build order)

```
E3 (discovery→topology) ──┐
E2 (traps→events) ────────┼─▶ E1 (multi-cond + dependency suppression) ──▶ E8 (correlation/AIOps)
                          │                                              │
E5 (RBAC/multitenant) ────┴─▶ E9 (on-call/incident/ITSM)  E12 (NetFlow elevation) ─┘
E6 (distributed/HA) ──▶ E7 (agent fleet) ──▶ E13 (cloud/k8s)
E4 (NCM) ──▶ E4p2 (push/compliance/firmware-EoL)
E10 / E11 / E14 / E15 mostly independent (sequence by market demand)
```

## Top risks & mitigations
- **Scope creep on AIOps (E8).** Mitigate: ship statistical baselining + correlation + forecasting first;
  add GenAI copilot as a thin, grounded layer (RAG over our own data + MCP), not a from-scratch ML platform.
- **Multi-tenancy retrofit (E5).** Touches every query. Mitigate: introduce a tenant scope column +
  row-level enforcement early; gate behind a feature flag; migration tested on a copy.
- **Distributed/HA (E6) data consistency.** Mitigate: collectors are stateless forwarders to ClickHouse;
  config pulled from API; HA = active/standby poller with Redis lease/lock first, true clustering later.
- **NCM credential blast radius (E4).** Mitigate: reuse existing encrypted Credentials + `core/crypto.py`;
  read-only backup before any push; push gated by RBAC + approval + dry-run diff.
- **Finishing-the-80% reveals data-model debt.** Mitigate: Phase-1 epics each include a short schema audit
  task before UI work.

## KPIs to track
- Deal-blocker checklist coverage (multi-cond ✓, SSO ✓, multi-tenant ✓, HA ✓, NCM ✓).
- Mean alerts per incident (target ↓ via suppression/correlation) and false-positive rate.
- Time-to-first-map after install (onboarding speed vs Auvik's <1 hr).
- % devices with config backup + drift detection (NCM adoption).
- Agent-monitored hosts; tenants live; collectors deployed per install.
