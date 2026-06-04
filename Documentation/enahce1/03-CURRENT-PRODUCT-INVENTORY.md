# ZenPlus — Current Product Inventory (Evidence-Based)

> Baseline as of 2026-06-03. Sources: live deployment `http://10.12.50.81/` (admin) +
> code in `/home/zen/zen-mon-push` (`server/` FastAPI, `poller/` Go, `dashboard/` React).
> **Note:** the live deployment is *ahead* of the local `zen-mon-push` repo — it additionally
> ships **Servers, NetFlow, Agent Fleet, Agent Policies, Automated Maps, Channels** which are
> not yet in this repo's `dashboard/src/App.tsx` routes (built on the build box / newer branch).
> This document is the authoritative "what we have today" used by the competitive analysis.

---

## 1. Product at a glance

- **Name:** ZenPlus — Network Monitoring System.
- **Architecture:** Go poller (ICMP/SNMP/service checks, single raw socket, 10k+ devices) →
  ClickHouse (metrics, TTL/rollups) + PostgreSQL (config/state) + Redis (realtime pub/sub) →
  FastAPI API (REST + WebSocket/SSE) → React/TypeScript dashboard (dark NOC UI).
  Evidence: `Documentation/01-PROJECT-OVERVIEW.md`, `poller/internal/**`, `server/app/**`.
- **Deployment:** single-line installer on Ubuntu (`install.sh`), systemd + Nginx, `zenplus` CLI,
  built-in **OTA update** system (`server/app/api/v1/system_updates.py`, Settings ▸ Updates),
  built-in **Licensing** (Settings ▸ Licenses) and **Subscription** page.
- **Live scale in the demo:** 35 network/security devices across 4 sites, 5 groups;
  NetFlow ingesting ~28.7 GB / 1.6M flows in 1h from 4 exporters.
- **Sidebar IA (live):** Dashboard · Devices · Servers · Services · NetFlow · Discovery ·
  Agent Fleet · Agent Policies · Automated Maps · Alerts · Alert Rules · Channels · Gateways ·
  Credentials · MIB Library · Users · Settings · Reports · Subscription.

---

## 2. Module-by-module inventory

### 2.1 Monitoring Overview (Dashboard) — STRONG
Live KPIs (total/online devices, critical alerts, avg uptime, locations/groups), **Global Network
Map** (geo, 4 locations, online/warning/offline), **Network Performance** (avg in/out/latency, avg/peak
toggle), **Health Status** + top warning devices, **Device Availability** by group/site, **Recent
Alerts** table, **Top Interfaces by Utilization**, **System Resources** (CPU/mem/storage). Elastic time
filter 1H/6H/24H/7D/30D, refresh every 15s ("Live"). Code: `pages/DashboardPage.tsx`,
`components/dashboard/*`.

### 2.2 Devices — STRONG
List with status/CPU/memory/uptime(24h)/last-seen; filters status/type/location + uptime range;
**Export**, **Add Device**, **Bulk Actions**, **Columns** customizer, **More filters**, server-side
pagination. Side panels: Device Distribution by Type, Availability by Location, Availability by Type,
Devices by Status, Recent Device Activity, Quick Actions (Add / **Discover Network** / **Import Devices**
/ Bulk Actions). Code: `pages/DevicesPage.tsx`, `api/v1/devices.py`, `services/device_service.py`.

### 2.3 Device Detail — STRONG
Header (vendor/model "Palo Alto", OS, location); time range 1h/24h/7d/1M/Custom; actions **Edit
Device**, **Run Diagnostics**, **Open Console**, Delete. Metric cards: CPU, Memory, Interface
Utilization, Latency, Packet Loss, Uptime. **Performance Overview** chart (CPU/Memory avg/max).
**Interface Status** table (13 ifaces: status/speed/in/out/err/util) + dedicated Interfaces page.
**Health Score** (0–100, configurable, "Excellent/87") with details drill-in. Device Inventory &
Configuration (Mgmt IP, **SNMP v3 · port 161**, Ping enabled · 60s, vendor/model, System OID, hardware
components count, tags). Recent Events / Activity Log (status changes, reboots, **SNMP traps**).
Environmental / System Stats (sensors, throughput). Quick Actions: **Ping Test**, **SNMP Test**,
**Backup Config**, **Acknowledge**. Code: `pages/DeviceDetailPage.tsx`, `DeviceInterfacesPage.tsx`,
`poller/internal/checker/snmp/*`, `models/device_interface.py`, `models/device_sensor.py`.

### 2.4 SNMP stack — STRONG
Go SNMP collector with sessions, OIDs, crypto, traps, profiles: `poller/internal/checker/snmp/`
(`collector.go`, `oids.go`, `session.go`, `traps.go`, `profile.go`, `crypto.go`). SNMP **v1/v2c/v3**
(v3 in use live), credential storage with reveal + crypto at rest (`api/v1/snmp_credentials.py`,
`core/crypto.py`), SNMP probe diagnostics, **MIB Library** page, **SNMP Profiles** page. Interface
counters, hardware health (CPU/mem), sysOID-based vendor/model classification, sensors/environmental.

### 2.5 Services / Service Checks — STRONG
Service monitoring end-to-end: **Service Checks** with **Groups**, **Templates**, **Maintenance**
windows, **Incidents**, SLA, multi-status checks, rich detail page. Probes: HTTP/HTTPS, TCP, DNS, TLS
(SSL expiry). Code: `pages/Services*.tsx`, `ServiceCheck*.tsx`, `api/v1/service_checks.py`
(+ `groups_router`, `maintenance_router`, `templates_router`), `services/service_check_service.py`,
`poller/internal/checker/{http,tcp,dns,tls,icmp}.go`.

### 2.6 NetFlow — STRONG / DIFFERENTIATOR
Live flow telemetry dashboard: Total Traffic, Active Flows, Top Devices, Top Applications, Active
Sessions; **Traffic Throughput** chart; **7-day Traffic Heatmap**; **Flow Signals** (flow duration,
RST ratio, exporting devices); **Top Talkers / Top Endpoints / Top Applications / Protocol Distribution
/ Top Conversations**; **Recent Flow Records** table; **DSCP/QoS classes**; **TCP Flags** (SYN-only scan
share, RST, FIN, etc.); **Network Class Mix** (private/public/CGNAT); **Top Interfaces** across exporters;
**Alerts/Incidents** (stale exporter, throughput, privileged-port traffic, large-flow). Sub-pages:
**Forensics search**, **Anomaly Detection**, **Capacity Planning**, **Saved Views**, per-exporter device
pages. App classification by port/heuristic ("Web", "Network Mgmt", "File Transfer", "Email", "Syslog").

### 2.7 NetFlow Anomaly Detection — STRONG / DIFFERENTIATOR
Flow-derived security signals with explicit algorithms: **SYN scan, RST flood, sensitive-port egress,
ICMP flood, volumetric outliers**. Findings include source, destinations touched, ports, flow/byte
counts, severity, and one-click **Investigate → Forensics** pivot. (Heuristic/signature-based, not ML
baselining — see gaps.)

### 2.8 Discovery — GOOD
Scan profiles bundling **scan scope + credentials + import rules**; **Scheduled** scans; **Reports**;
**Import Queue**; **Ignored**; **Credentials**. Auto-classify + import into monitoring. Code:
`pages/DiscoveryPage.tsx`, `api/v1/discovery.py`. (No L2/L3 topology *drawing* from discovery yet —
that lives in Automated Maps.)

### 2.9 Automated Maps — GOOD (links not yet populated live)
**LLDP/CDP topology**, **dependency mapping**, and **upstream-aware alert suppression** (parent
failures suppress child alerts). Controls: Discover LLDP/CDP, Dependency, add critical uplinks manually,
unresolved-neighbors reconciliation. Counters for Links/Dependencies/Suppression/Suppressed-24h/Unmapped.
Live: 35 nodes, 0 observed links (LLDP/CDP not yet run on demo).

### 2.10 Servers (host monitoring) — BUILT, EARLY
Monitored hosts via **local agents or agentless WMI/WinRM/SNMP/SSH**; collects CPU/mem/disk/network/
**Windows service** telemetry; top CPU/mem/disk/throughput pressure cards; filters status/OS/collection.
Live: 0 servers enrolled (feature shipped, not populated).

### 2.11 Agent Fleet & Agent Policies — BUILT, EARLY
**Agent Fleet:** operate installed host agents — bulk change policy, **ring** (staged/canary rollout),
trigger upgrades; filter by status/platform/ring. **Agent Policies** for config. Live: 0 agents enrolled.

### 2.12 Alerting — GOOD (single-condition)
**Alert Rules:** metric + operator + threshold, **hold duration** (flap protection), **cooldown**,
scope = **device / group / service check**, severity, enable toggle. **Alerts** list with severity,
device, message, time. Engine: `api/v1/alert_rules.py`, `alert_engine.py`, `alerts.py`,
`services/alert_service.py`, `models/alert.py`. Limitation: **single condition per rule** (no
compound/multi-metric, no anomaly/baseline, no escalation policy / on-call schedule in the rule).

### 2.13 Channels & Gateways — GOOD
Connector catalog: **Email/SMTP, SMS, Webhook, Slack, Telegram = Ready**; **Microsoft Teams,
Jira/ServiceNow = Planned**. Routing lanes: **Alerts / Reports / Tickets**. **Gateway layer**: SMTP/SMS
gateways (`pages/GatewaysPage.tsx`). Notifications page. ITSM ticketing = planned lane.

### 2.14 Reports — STRONG
Four personas: **Executive** (leadership), **Technical** (engineers), **Business** (service owners),
**Inventory** (assets); **PDF / Excel / CSV export**; time range; KPIs Availability, Active Critical,
**MTTR**, Devices Monitored, **SLA (target 99.9%)**, Incidents; Availability Trend; Top Issues; Health by
Location; Recent Outages. Code: `pages/reports/*`, `api/v1/reports.py`, `services/report_service.py`,
`report_data_service.py`, `export_service.py`. (Scheduled email delivery appears not yet wired —
"Reports: Ready for assignment" on Channels.)

### 2.15 Administration — GOOD
**Users** (RBAC via `api/v1/users.py`, `models/user.py`, `core/security.py`), **Settings**
(Company, SMTP/Email, Appearance, **Licenses**, **Updates/OTA**, **Sensors**, Support, Profile),
**Credentials**, **Subscription/Licensing**. No SSO/SAML/OIDC or multi-tenancy observed.

---

## 3. Capability self-assessment vs. the analysis taxonomy

Levels: ✅ full · 🟡 partial/shallow · ❌ none.

| Code | Capability | Level | Evidence / note |
|------|------------|:----:|-----------------|
| A1 | Auto discovery | 🟡 | Discovery profiles + scheduled + import queue; no topology-seeded crawl drawing yet |
| A2 | L2/L3 topology mapping | 🟡 | Automated Maps via LLDP/CDP; links not populated, no L3/route map |
| A3 | Inventory / asset | ✅ | Devices + Inventory report + device inventory panel |
| A4 | Hardware health (SNMP) | ✅ | CPU/mem live; sensors/environmental model present (temp/fan/PSU shallow) |
| B1 | ICMP ping (RTT/loss/jitter) | ✅ | Go pinger raw socket; latency/loss live |
| B2 | SNMP polling (v1/2c/3, OIDs) | ✅ | Full SNMP stack incl v3, custom OIDs, profiles |
| B3 | SNMP traps | 🟡 | `traps.go` + activity log surfaces traps; trap→alert mapping shallow |
| B4 | Interface bandwidth/errors | ✅ | Interface table in/out/err/util/speed |
| B5 | Service/port checks | ✅ | HTTP/TCP/DNS/TLS + SSL expiry, groups/templates/SLA |
| B6 | Synthetic / transactions | ❌ | HTTP up/down only; no multi-step web journeys |
| B7 | Streaming telemetry (gNMI) | ❌ | none |
| C1 | Flow ingest (NetFlow/sFlow/IPFIX) | ✅ | NetFlow dashboard live with exporters |
| C2 | Top talkers/apps/conversations | ✅ | Full set incl endpoints/conversations |
| C3 | DPI / app recognition | 🟡 | Port/heuristic classification, not true DPI/NBAR |
| C4 | Capacity / traffic forecasting | 🟡 | NetFlow Capacity Planning page; forecasting depth TBD |
| D1 | Config backup / versioning / diff | 🟡 | "Backup Config" action on device; no version/diff UI yet |
| D2 | Config push / change automation | ❌ | none |
| D3 | Compliance / firmware/EoL/vuln | ❌ | none |
| E1 | Topology maps (auto+manual) | 🟡 | Automated Maps exists; manual editing/weather-map limited |
| E2 | Geographic maps | ✅ | Global Network Map on overview |
| E3 | Custom dashboards / NOC walls | 🟡 | Rich fixed dashboards; no user-built drag/drop widgets |
| E4 | Business-service / dependency map | 🟡 | Dependency mapping in Automated Maps; no business-service modeling |
| F1 | Wireless (controllers/AP/heatmaps) | ❌ | none |
| F2 | SD-WAN monitoring | ❌ | none |
| F3 | Cloud infra (AWS/Azure/GCP) | ❌ | none |
| F4 | Internet/ISP/WAN path (BGP, hop) | ❌ | none (flow public/CGNAT mix only) |
| G1 | Threshold / multi-condition rules | 🟡 | Single-condition only; duration+cooldown present |
| G2 | Anomaly / ML baselining | 🟡 | Heuristic flow anomalies only; no metric ML baselining |
| G3 | Dependency / parent-child suppression | 🟡 | Built in Automated Maps; not exercised (0 links) |
| G4 | Dedup / correlation / flapping | 🟡 | Cooldown/hold; no event correlation/dedup engine |
| G5 | Maintenance windows | ✅ | Service maintenance windows |
| G6 | Escalation / on-call schedules | ❌ | none |
| G7 | Notification channels | ✅ | Email/SMS/Webhook/Slack/Telegram; Teams/ITSM planned |
| G8 | Incident mgmt / ack / ITSM | 🟡 | Service incidents + ack; ticketing planned |
| H1 | Server/host agent | 🟡 | Servers + Agent Fleet built; 0 enrolled, agent maturity TBD |
| H2 | App/DB/middleware/container | ❌ | none (Windows services only, early) |
| H3 | Logs / SIEM | ❌ | none (Syslog seen only as flow app) |
| H4 | APM / traces / RUM | ❌ | none |
| H5 | Kubernetes / containers | ❌ | none |
| I1 | Custom reports + export | ✅ | 4 personas, PDF/Excel/CSV |
| I2 | SLA / uptime reporting | ✅ | SLA target, availability, MTTR |
| I3 | Capacity planning / forecasting | 🟡 | NetFlow capacity page; device-metric forecasting absent |
| I4 | AI/ML / AIOps / GenAI assistant | 🟡 | Flow heuristics only; no predictive/RCA/GenAI |
| I5 | Executive / business dashboards | ✅ | Executive + Business reports |
| J1 | RBAC / SSO / multi-tenancy | 🟡 | Users/roles; no SSO/SAML/OIDC/multi-tenant |
| J2 | Scalability (distributed pollers) | 🟡 | Single poller design 10k+; multi-collector/sharding TBD |
| J3 | HA / failover | ❌ | none documented |
| J4 | REST API / IaC | 🟡 | FastAPI REST + OpenAPI; no terraform/provider |
| J5 | Integrations ecosystem | 🟡 | Channels connectors; no marketplace/plugin SDK |
| J6 | Deployment options | 🟡 | On-prem/appliance + OTA; no managed SaaS multi-tenant |
| J7 | Mobile app | ❌ | none (responsive web only) |
| J8 | Pricing transparency | 🟡 | Subscription/licensing built-in; public model TBD |

**Headline:** ZenPlus is already strong on **ICMP+SNMP availability/performance, service checks,
NetFlow analytics (incl. flow-based security signals), geo + health dashboards, multi-persona reporting
with export, and an appliance + OTA delivery model**. The biggest open fronts are **NCM, wireless,
cloud/hybrid, ML/AIOps baselining + correlation, escalation/on-call + ITSM, SSO/multi-tenant, and
maturing the server-agent fleet**.
