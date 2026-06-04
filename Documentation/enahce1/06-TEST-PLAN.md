# ZenPlus — Consolidated Test Plan (enahce1)

*Cross-cutting QA strategy for the enhancement roadmap. Per-epic test-case tables live in each
`05-EPIC-*` document (section 11). This file defines the overall approach, environments, regression
suite, and the cross-epic test matrix.*

## 1. Test strategy & levels

| Level | Scope | Tooling (suggested) | Owner |
|-------|-------|---------------------|-------|
| **Unit** | Pure logic: alert condition eval, diff engine, baseline math, flow classifiers, RBAC checks | Go `testing`/testify; Python `pytest`; Vitest (TS) | Dev |
| **Integration** | API ↔ Postgres/ClickHouse/Redis; poller ↔ stores; trap/flow ingestion | pytest + ephemeral DBs (docker-compose); Go integration tags | Dev |
| **Contract** | REST schema stability (OpenAPI), poller↔API messages | schemathesis against FastAPI OpenAPI | Dev |
| **E2E / UI** | User journeys across dashboard | Playwright (already used in repo `.playwright-mcp/`) | QA |
| **Performance/scale** | 10k+ devices, flow throughput, query latency, collector fan-out | k6 / custom load gen; ClickHouse query benchmarks | Perf |
| **Security** | RBAC/tenant isolation, secret handling, SSO, NCM credential blast radius | manual + automated authz tests; ZAP for web | Sec |
| **Acceptance/UAT** | Per-epic acceptance criteria (section 10 of each epic) | scripted manual + Playwright | PM/QA |

## 2. Test environments

- **CI ephemeral:** docker-compose bringing up Postgres + ClickHouse + Redis + API + poller; seeded fixtures.
- **Staging appliance:** full OTA appliance image mirroring production install (`install.sh`) — validates
  migrations, OTA upgrade path, and systemd/nginx wiring.
- **Scale lab:** synthetic device/flow generators (simulate 10k SNMP devices + N flow exporters) for E6/E8/E12.
- **Live-like demo:** the 35-device, 4-site dataset on `10.12.50.81` is the reference for visual/UX regression.

## 3. Cross-cutting (non-functional) requirements — every epic must pass

- **Multi-tenancy isolation (after E5):** no query may return another tenant's devices/alerts/flows/configs.
  Add an automated authz test that runs each new endpoint as Tenant-A and asserts zero Tenant-B leakage.
- **RBAC enforcement:** every new mutating endpoint checks role/permission; viewer role is read-only.
- **Backward compatibility:** DB migrations are forward-only, reversible in staging, and idempotent on OTA.
- **Performance budgets:** API p95 < 400 ms for list endpoints at 10k devices; dashboard first-paint < 2.5 s;
  ClickHouse aggregation queries < 1 s for 1h windows.
- **Realtime:** WebSocket/SSE updates within 15 s (matches the "refreshes every 15s" promise).
- **Secret safety:** credentials/config never logged or returned in plaintext (reuse `core/crypto.py`); NCM
  and SNMPv3 secrets encrypted at rest; "reveal" gated by permission + audit.
- **OTA upgrade:** every release upgrades cleanly from the prior version with data intact (test on staging).

## 4. Regression suite (run every release)

1. **Smoke:** login (admin/admin123 + SSO), dashboard loads, device list, device detail, NetFlow, reports.
2. **Core monitoring:** add device → poll → metrics appear; service check → status; SNMP v2c/v3 poll; trap received.
3. **Alerting:** single + multi-condition rule fires; cooldown/hold respected; channel delivery; suppression.
4. **Data integrity:** uptime %, SLA, MTTR, availability-by-site numbers match between Overview / Devices / Reports.
5. **Export:** PDF/Excel/CSV for each report persona open and contain data.
6. **Permissions:** viewer cannot mutate; tenant isolation holds; SSO login + logout.
7. **Upgrade:** OTA from N-1 → N preserves devices, history, credentials, dashboards.

## 5. Cross-epic test matrix (representative; full cases in each `05-EPIC-*` §11)

| Area | Critical scenarios |
|------|--------------------|
| **E1 Alerting** | AND/OR compound fires only when all/any sub-conditions true; parent-down suppresses children; flap (hold) blocks brief blips; cooldown blocks re-fire; routing to correct channel. |
| **E2 Traps** | Valid trap → event row + MIB translation; unknown OID → raw event; trap rule fires alert; malformed/oversized trap dropped safely; trap storm rate-limited. |
| **E3 Discovery/Topology** | Scan range discovers + classifies; LLDP/CDP builds links; auto-import respects ignore rules; re-scan is idempotent; unmapped neighbor resolves. |
| **E4 NCM** | Scheduled backup stores version; unchanged config = no new version; change → diff + alert; restore/push gated by RBAC + dry-run; encrypted at rest; multi-vendor (Cisco/PAN/FortiGate). |
| **E5 RBAC/SSO/Tenant** | SAML/OIDC login + role mapping; viewer read-only; **no cross-tenant leakage on every endpoint**; admin scoping; session/expiry. |
| **E6 Distributed/HA** | Collector enrolls + heartbeats; device assigned to one collector; collector offline → reassign/store-and-forward; poller standby takes over on primary failure with no double-write. |
| **E7 Agents** | Agent enrolls into ring; policy push; staged upgrade canary→broad; Windows + Linux metrics; agentless WMI/SSH fallback; offline agent detected. |
| **E8 AIOps** | Baseline learns; anomaly fires outside band, not inside; correlation groups related alerts; forecast runout sane; GenAI copilot answers grounded (no hallucinated devices) + MCP read-only. |
| **E9 Incident/ITSM** | Escalation steps fire on no-ack; on-call rotation resolves correct person; Jira/ServiceNow ticket created + synced; Teams card delivered; ack stops escalation. |
| **E10 Dashboards** | Build/save/share dashboard; widget binds to live data; variable filter; kiosk playlist cycles; permission on shared dashboard. |
| **E11 Synthetic/Path/BGP** | Multi-step journey assertions pass/fail; SSL expiry warns; traceroute renders per-hop loss/latency; ISP-vs-internal attribution; BGP route-change alert. |
| **E12 NetFlow/NDR** | ML app-ID beats port heuristic on encrypted flow; forecast per interface; DDoS/volumetric baseline triggers; NDR finding correlates into alert; forensics pivot retains filter. |

## 6. Definition of Done (per epic)

- All acceptance criteria (epic §10) pass in staging on the appliance image.
- Unit + integration coverage for new logic; E2E for the primary user journey.
- Non-functional cross-cutting checks (§3) green: RBAC, tenant isolation, perf budget, secret safety, OTA upgrade.
- Regression suite (§4) green. Docs + release notes updated. Feature behind a flag until UAT sign-off.
