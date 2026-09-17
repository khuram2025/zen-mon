# ZenPlus vs SolarWinds NPM

Network device monitoring: feature assessment and delivery plan

10 September 2026

ZenPlus v1.23.10 | Development appliance | Revision 2: environment corrected

## Executive assessment

Assessment: ZenPlus has a substantial network-monitoring implementation with identifiable feature-depth gaps against NPM. This is a development capability review, not a production-readiness verdict. Real-device correctness and scale remain untested.

Existing foundations include ICMP/SNMP collection, vendor templates, interface and hardware panels, NetPath, manual maps, alert configuration and report templates. Simulator records demonstrate UI and implementation surfaces, not real-device certification.

Development conditions are not product defects: no real SNMP devices, zero traffic, short history, offline test sensors and unconfigured channels are excluded from the feature-gap judgment. The previous revision gave these conditions excessive operational weight.

Remaining product gaps include topology lifecycle and routing depth, Nexus/ACI and cloud integrations, wireless client/RF visibility, F5 relationships, forecasting, dynamic baselines, a shared investigation timeline, compiled MIB tooling and syslog. Alert-evaluator source findings require reproduction; synthetic health discrepancies require a design/fixture review.

Plan: first confirm implementation gaps with deterministic fixtures and code tests, then build prioritized features. Real-device certification is a separate stage when equipment is available. The previous 8-12 / 20-28 week windows remain provisional scenarios pending this triage and test access, not revised commitments.

| Status | Rows |
|---|---:|
| Available / unproven | 17 |
| Observed | 8 |
| Unverified | 4 |
| Partial | 25 |
| Not found | 17 |

71 capability rows. Counts are not a parity percentage.

## Scope and method

Revision 2 / user clarification: this is a development appliance without live SNMP or real network devices. The original review recognized simulator records but did not consistently apply that limitation. This revision supersedes its operational-risk framing; it does not add new runtime tests.

Live scope: existing administrator session on the requested appliance, observed on 10 September 2026 at approximately 15:27-15:41 Asia/Riyadh. Read dashboards, tables, detail views and unsaved editors. No device settings, rules, credentials, deployments, discovery scans, packet captures or outgoing test messages were changed or triggered.

Source corroboration: local C:/Users/user/Documents/ZenPlus checkout, .version 1.23.10 and HEAD b4256ff. The working tree contained unrelated modifications. Same version labels do not prove deployed binary/source identity; source findings are explicitly provisional for the appliance until deployment hash and runtime behavior are verified.

Comparator: full NPM functional scope and its shared SolarWinds Platform features, checked against current official documentation and NPM 2026.2 release notes. The directly opened documentation index advertised 2026.2.2; a search cache advertised 2026.2.1. This report uses feature documentation, not a claim that all patch-specific changes were validated. [[S03](https://documentation.solarwinds.com/en/success_center/npm/content/npm_documentation.htm?id=15032385540) [S04](https://documentation.solarwinds.com/en/success_center/npm/content/release_notes/npm_2026-2_release_notes.htm)]

License boundaries: NTA, NCM, UDT, IPAM, VNQM and EOC are not silently counted as NPM. HA and additional polling engines have deployment/licensing dependencies. Newer fleet Routing Insights explicitly requires Observability Self-Hosted; it is labeled Extension. Validate current entitlements before procurement or a contractual parity claim. [[S17](https://documentation.solarwinds.com/en/success_center/npm/content/npm-routing-insights-home.htm) [S26](https://documentation.solarwinds.com/en/success_center/orionplatform/content/orion_platform_scalability_engine_guidelines.htm?CMP=DIRECT&CMPSource=THW) [S28](https://documentation.solarwinds.com/en/success_center/nam/content/nam_administrator_guide.htm)]

Coverage: the matrix is an operational capability checklist across the documented NPM feature families, not a certification of every OID, supported device SKU, firmware release, integration action or deployment size. SolarWinds itself was assessed from official documentation, not a parallel live installation.

Evidence limits: synthetic or empty data cannot establish collector failure, missing functionality, production outages or poor availability. No fault injection, real-device polling, delivery or failover test was performed. Source-backed omissions and explicit deferred features are distinguished from implementation that simply awaits testing.

Status counts measure evidence categories, not completion or parity percentages. Available / unproven and Unverified are validation items, not missing-feature counts. Real-device accuracy, sustained reliability and scale are reserved for later certification.

### Status definitions

- **Observed:** The narrow feature was visible with current records or concrete controls. This is not an end-to-end parity certification.
- **Partial:** Implemented with a specific feature-depth gap or source-backed semantic concern. Empty development data alone is not grounds for this status.
- **Available / unproven:** An implementation or workflow is visible, but behavior has not been exercised. Unconfigured or empty development state is not a defect.
- **Not found:** No implementation was located in the inspected UI and scoped source review, or the UI explicitly labels it deferred/planned. This is an evidence-bounded finding.
- **Unverified:** The inspection cannot determine working support or its limits. A dedicated acceptance test is required.

P0 = candidate requiring reproduction; P1/P2 = feature-depth priority; Validation = testing task, not a gap. A-H = workstream; E = UI evidence; C = source evidence; S = SolarWinds source.

## Priority findings

### Environment | Development conditions excluded from gaps

The user confirmed there are no live SNMP or real network devices on this appliance. Empty channels, short/synthetic history, zero throughput, no discovery runs and an offline test sensor are configuration/fixture conditions. They do not justify P0 product findings, a production outage claim or a restore-service workstream. Evidence E01-E04/E10/E13/E15.

### P0 candidate | Compound alert evaluation

Local source C04 mirrors conditions[0] into legacy fields; C03 reads compound fields but the reviewed periodic branches evaluate the legacy metric. CPU >90 AND memory >90 could therefore use only the first condition. This concern is independent of real SNMP availability. Verify deployed identity and reproduce with injected metric truth tables before confirming a defect.

### P0 candidate | Canonical state metric mapping

fan_state, psu_state, vpn_tunnel_state, ha_state and bgp_neighbor_down are accepted by the rule API but absent from the periodic canonical supported set. tpl_* is an alternative, so this is not absence of all component alerts. Trace accepted definitions through evaluation using fixtures; confirm or dismiss each mismatch. Evidence C03/C04.

### P0 candidate | Hold and dependency semantics

Reviewed template evaluation uses latest values in a lookback, which does not establish a continuous breach. Periodic SNMP paths do not show the dependency checks used by event-driven alerts. Test hold/reset/no-data and parent-child cases with a controlled clock and injected state. Confirm deployed behavior before labeling appliance defects. Evidence C03/C05.

### P1 review | Health score and stale demo semantics

Synthetic component faults alongside 100/100 and an old demo NetPath OK label are design/fixture questions. The score is described as CPU/memory/loss, so component faults need not imply a calculation bug. Agree labels, roll-up policy and demo behavior; reproduce using coherent enabled fixtures before requesting a fix. Evidence E02/E05/E08.

### Feature gaps | Explicit missing implementation remains relevant

Storage-only MIB uploads, absent exhaustion forecasts and the missing wireless-client/RF, cloud-controller, syslog and shared-timeline workflows do not depend on populated telemetry. Prioritize these based on source/UI evidence and target vendor scope. Automatic topology and vendor relationships have partial foundations rather than confirmed full coverage.

### Validation | Real-device interoperability remains unknown

Existing collectors and vendor packs cannot be certified from UI examples. Later compare SNMPv2c/v3, counters, traps, interface identity, hardware and protocol states against representative hardware or vendor virtual appliances. Lack of such access is a test prerequisite, not evidence that the features are missing.

### Validation | Reliability and scale require a separate stage

Coverage, notification delivery, collector continuity, access isolation, export accuracy, HA and capacity need dedicated tests. First use deterministic simulators and local test sinks; schedule real-device/scale qualification when available. These are future release criteria, not current development-appliance deficiencies.

## Feature-by-feature matrix

### Discovery and core polling

| ID / feature | SolarWinds baseline / scope | Status | ZenPlus evidence and gap | Priority / workstream |
|---|---|---|---|---|
| F01 **Network discovery and classification** | Network discovery; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Available / unproven | Discovery wizard and backend exist. Empty development history is expected; validate discovery lifecycle with fixtures, then a controlled subnet when available. **Evidence:** E04 C07. | Validation / A |
| F02 **Recurring discovery and import policy** | Scheduled discovery; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Available / unproven | Recurring/cron and review/auto-match implementation exists. No configured development profiles is not a feature gap; test scheduler and import policy with controlled fixtures. **Evidence:** E04 C07. | P1 / B |
| F03 **Resource selection and bulk management** | Manage nodes and resources; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Available / unproven | Device/interface selection, filtering and exports present; verify rediscovery preserves interface identity and monitoring choices. **Evidence:** E01 E03 E04. | P1 / B |
| F04 **ICMP status, RTT and packet loss** | Availability and response; NPM. [S05](https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-choosing-your-polling-method-sw1223.htm?cshid=OrionCoreAG_ChoosingPollingMethods) | Observed | Status/history surfaces are present. Development availability values are not a production SLA result; later test missing-check and denominator policy. **Evidence:** E01 E02 E15. | Validation / A |
| F05 **SNMP v1/v2c/v3 polling** | SNMP polling; NPM. [S05](https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-choosing-your-polling-method-sw1223.htm?cshid=OrionCoreAG_ChoosingPollingMethods) | Available / unproven | v2c simulator records and v3 model/session support exist. AuthPriv/context interoperability needs real-device or vendor-virtual-device certification; no failure inferred. **Evidence:** E02 E07 C01. | P1 / B |
| F06 **IPv6 network monitoring** | IPv6-capable monitoring; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Unverified | IPv6 discovery, ICMP, SNMP and tables require certification. Local NetPath is IPv4-only; this report does not assume SolarWinds NetPath supports IPv6. **Evidence:** C02 C06. | P1 / B |
| F07 **CPU, memory, uptime and identity** | Device performance; NPM. [S01](https://www.solarwinds.com/network-performance-monitor) | Observed | Vendor and standard metrics appear on simulator records. Real-device numerical accuracy remains a separate certification task. **Evidence:** E02 E07 E08. | Validation / A |
| F08 **Polling intervals and diagnostics** | Configurable polling; NPM. [S05](https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-choosing-your-polling-method-sw1223.htm?cshid=OrionCoreAG_ChoosingPollingMethods) | Available / unproven | Ping interval, SNMP diagnostics and bounded table cadence exist. Fleet poll completion, timeout causes and stale-resource health need validation. **Evidence:** E02 C01 C07. | P1 / B |
| F09 **Historical data and coverage** | Real-time and historical views; NPM. [S01](https://www.solarwinds.com/network-performance-monitor) | Available / unproven | Historical charts and coverage disclosure exist. Short development history does not establish a retention or collection defect; test missing-data semantics and later sustained continuity. **Evidence:** E02 E15. | Validation / A |
| F10 **Tags, groups and custom properties** | Group and property scoping; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Partial | Tags/location/groups and tag-limited visibility exist. Arbitrary typed properties and dynamic group rules were not demonstrated. **Evidence:** E01 E16. | P2 / B |

### Interfaces and device hardware

| ID / feature | SolarWinds baseline / scope | Status | ZenPlus evidence and gap | Priority / workstream |
|---|---|---|---|---|
| F11 **Interface inventory and status** | Interface monitoring; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Observed | 47 links and per-device tables visible, including speed and status. Retain this foundation; validate reindex and administrative-down semantics. **Evidence:** E03. | P1 / B |
| F12 **Bandwidth and utilization** | Receive/transmit utilization; NPM. [S01](https://www.solarwinds.com/network-performance-monitor) | Available / unproven | 64-bit counters and rate handling exist. Zero/dashed development throughput is expected without traffic; verify calculations with synthetic counter sequences, then known device traffic. **Evidence:** E03 C01. | Validation / A |
| F13 **Errors and discards** | Interface health statistics; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Available / unproven | Fleet filters and reset-safe counter views exist; nonzero fault examples and rate/interval accuracy were not demonstrated. **Evidence:** E03 C01. | P1 / B |
| F14 **Link flaps and downtime** | Interface outage diagnostics; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Available / unproven | Flapping-link counters and detail views present. Test up/down sequences, missing samples and per-link timelines. **Evidence:** E03. | P1 / B |
| F15 **Custom interface capacity** | Per-interface configuration; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Observed | Manual speed/bandwidth control visible. Prove asymmetric circuit calculations and direction labels. **Evidence:** E03. | P1 / B |
| F16 **Duplex mismatch diagnostics** | Detect/predict mismatch; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Not found | No dedicated duplex state or mismatch diagnosis located. Add polling, peer correlation and actionable alerts for supported media. **Evidence:** E03 C01. | P2 / D |
| F17 **Physical device / port layout** | Device View stencils; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Not found | Interface tables are present; chassis/slot/port stencils with live status were not found. **Evidence:** E02 E03. | P2 / D |
| F18 **Temperature, fans and PSU** | Hardware health; NPM. [S27](https://documentation.solarwinds.com/en/success_center/npm/content/onboarding/npm-ob-beyond-getting-started.htm) | Partial | Hardware panels exist. Synthetic faults alongside a 100/100 performance score raise a roll-up/labeling design question, not proof of failed physical sensors. Reproduce with coherent fixtures and agree score scope. **Evidence:** E02 E07 E08. | P1 review / A |
| F19 **Switch stacks and redundancy** | Stack monitoring; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Partial | Cisco members/ring observed; CX VSF pack exists. Juniper Virtual Chassis and complete cross-vendor stack health not demonstrated. **Evidence:** E02 E07 C11. | P1 / D |
| F20 **PoE, optics and modular hardware** | Device-specific hardware; NPM. [S04](https://documentation.solarwinds.com/en/success_center/npm/content/release_notes/npm_2026-2_release_notes.htm) | Partial | PoE visible and MikroTik optics defined. Broader optics, line-card state, reset reasons and inventory relationships need coverage. **Evidence:** E02 E07 C11. | P2 / D |

### Topology and network protocols

| ID / feature | SolarWinds baseline / scope | Status | ZenPlus evidence and gap | Priority / workstream |
|---|---|---|---|---|
| F21 **Manual maps and NOC display** | Maps and NOC views; NPM. [S01](https://www.solarwinds.com/network-performance-monitor) | Observed | Rich editable map with live mode and interface labels exists. The inspected map was QA/simulator data. **Evidence:** E06. | P1 / B |
| F22 **LLDP/CDP topology collection** | Topology relationships; NPM. [S01](https://www.solarwinds.com/network-performance-monitor) | Available / unproven | Quick-connect consumes discovered adjacency. Validate refresh, stale links, port resolution and multi-vendor discovery. **Evidence:** E06 C09. | P1 / D |
| F23 **Automatic topology lifecycle** | Dynamic topology mapping; NPM. [S01](https://www.solarwinds.com/network-performance-monitor) | Partial | Manual canvas assistance is not proof of automatically maintained topology. Add change history, aging and confidence for discovered links. **Evidence:** E06 C09. | P1 / D |
| F24 **Dependencies and impact isolation** | Dependency-aware alerting; NPM. [S01](https://www.solarwinds.com/network-performance-monitor) | Partial | Event-driven suppression exists; periodic SNMP parity and an operator-facing dependency workflow were not established. **Evidence:** E09 C05 C03. | P0 / C |
| F25 **BGP and OSPF neighbor health** | Routing neighbor visibility; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Partial | BGP/OSPF tables exist in collector; no complete routing workflow demonstrated. Canonical BGP alert key needs evaluator mapping. **Evidence:** E07 C02. | P1 / D |
| F26 **Routing tables and route changes** | Route visibility; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) [S17](https://documentation.solarwinds.com/en/success_center/npm/content/npm-routing-insights-home.htm) | Not found | Dedicated route inventory, next-hop correlation and route/default-route change views not located. Scope original NPM versus newer Insights separately. **Evidence:** C02 E06. | P1 / D |
| F27 **VRRP, STP and LACP health** | Network protocol context; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Partial | Standard tables implemented. Dedicated protocol relationships, transitions and object-level alert lifecycle still need verification. **Evidence:** C02. | P1 / D |
| F28 **Multicast groups and routing** | L3 multicast monitoring; NPM. [S16](https://documentation.solarwinds.com/en/success_center/npm/content/monitor-multicast-traffic.htm) | Not found | No multicast routing/group collector or tree workflow found. Flow protocol classification is not multicast routing monitoring. **Evidence:** E06 C02. | P2 / D |
| F29 **Fleet routing insights, VRFs and peer correlation** | Unified Routing Insights; Extension. [S17](https://documentation.solarwinds.com/en/success_center/npm/content/npm-routing-insights-home.htm) [S18](https://documentation.solarwinds.com/en/success_center/npm/content/npm-routing-neighbors.htm) | Not found | Candidate extension: no unified VRF/peer/route interface was found. Official fleet Routing Insights requires Observability Self-Hosted licensing. **Evidence:** C02 E06. | P2 / D |

### Vendor, wireless and hybrid coverage

| ID / feature | SolarWinds baseline / scope | Status | ZenPlus evidence and gap | Priority / workstream |
|---|---|---|---|---|
| F30 **Cisco ASA insight** | ASA Network Insight; NPM. [S27](https://documentation.solarwinds.com/en/success_center/npm/content/onboarding/npm-ob-beyond-getting-started.htm) | Partial | ASA pack includes 19 metrics for connections, failover and remote-access counts. Zero attached development devices is not a gap; required per-tunnel/user drill-down depth remains to be established. **Evidence:** E07. | P1 / E |
| F31 **Cisco Nexus vPC insight** | Nexus Network Insight; NPM. [S11](https://documentation.solarwinds.com/en/success_center/npm/content/npm-nexus-set-up-monitoring.htm) | Not found | No dedicated NX-OS/vPC collector or workflow located. IOS/XE metrics do not establish Nexus peer/keepalive/member-port parity. **Evidence:** E07 C11. | P1 / E |
| F32 **Cisco ACI logical / physical context** | ACI monitoring; NPM. [S27](https://documentation.solarwinds.com/en/success_center/npm/content/onboarding/npm-ob-beyond-getting-started.htm) | Not found | No APIC integration or tenant/application-profile/EPG/fabric workflow found. Build only against a representative fabric and fixed scope. **Evidence:** E07. | P2 / F |
| F33 **F5 LTM component monitoring** | F5 virtual servers and pools; NPM. [S12](https://documentation.solarwinds.com/en/success_center/npm/content/npm-monitor-load-balancers.htm) [S13](https://documentation.solarwinds.com/en/success_center/npm/content/npm-load-balancers-eventsalertsreports.htm) | Partial | 39-metric SNMP pack provides virtual-server/pool health, traffic, HA and hardware. Relationship graph and pool-member-level parity not demonstrated. **Evidence:** E07. | P1 / E |
| F34 **F5 DNS/GTM and health monitors** | GTM and iControl; NPM. [S12](https://documentation.solarwinds.com/en/success_center/npm/content/npm-monitor-load-balancers.htm) | Not found | No GTM/Wide-IP hierarchy or iControl health-monitor workflow located; optional pool-member rotation controls also absent. **Evidence:** E07. | P2 / E |
| F35 **Palo Alto firewall insight** | Palo Alto Network Insight; NPM. [S04](https://documentation.solarwinds.com/en/success_center/npm/content/release_notes/npm_2026-2_release_notes.htm) [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Partial | PAN-OS pack defines sessions, CPU, HA and GlobalProtect. Complete site-to-site tunnel state/traffic/encryption detail is not established. **Evidence:** E07. | P1 / E |
| F36 **FortiGate monitoring** | Fortinet network monitoring; NPM. [S04](https://documentation.solarwinds.com/en/success_center/npm/content/release_notes/npm_2026-2_release_notes.htm) [S08](https://documentation.solarwinds.com/en/success_center/npm/content/core-monitoring-wireless-networks-sw764.htm) | Available / unproven | Rich FortiGate SNMP panels and template implementation exist. Physical or virtual FortiOS validation is pending; simulator-only data does not reduce implemented coverage. **Evidence:** E08. | P1 / E |
| F37 **Juniper, Aruba, Dell, MikroTik and other vendors** | Multi-vendor device coverage; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) [S04](https://documentation.solarwinds.com/en/success_center/npm/content/release_notes/npm_2026-2_release_notes.htm) | Partial | Several vendor packs exist. Unassigned packs are not missing. Broader Arista/Extreme support was not established; assess family depth separately from model/firmware certification. **Evidence:** E07 C11. | P2 / E |
| F38 **Wireless controller and AP inventory** | Wireless monitoring; NPM. [S08](https://documentation.solarwinds.com/en/success_center/npm/content/core-monitoring-wireless-networks-sw764.htm) | Partial | Aruba/FortiGate AP and client totals exist. No unified wireless inventory or Cisco WLC monitoring pack demonstrated. **Evidence:** E07 E08. | P1 / E |
| F39 **Per-client wireless performance** | Client, SSID and radio detail; NPM. [S10](https://documentation.solarwinds.com/en/success_center/npm/content/npm-aruba-central-wireless.htm) | Not found | No client-level RSSI/SSID/radio/channel history located. Aggregate client counts and UDT endpoint records do not meet this capability. **Evidence:** E07 E08. | P1 / E |
| F40 **Rogue APs and RF coverage maps** | Wireless diagnostics; NPM. [S08](https://documentation.solarwinds.com/en/success_center/npm/content/core-monitoring-wireless-networks-sw764.htm) [S09](https://documentation.solarwinds.com/en/success_center/npm/content/core-adding-wireless-access-points-sw3344.htm) | Not found | No rogue-AP workflow or RF heat map found. SolarWinds heat maps have Cisco-controller restrictions and legacy Network Atlas dependencies. **Evidence:** E06 E07. | P2 / E |
| F41 **Cloud-managed wireless APIs** | Cloud controller monitoring; NPM. [S08](https://documentation.solarwinds.com/en/success_center/npm/content/core-monitoring-wireless-networks-sw764.htm) [S10](https://documentation.solarwinds.com/en/success_center/npm/content/npm-aruba-central-wireless.htm) | Not found | Meraki, Aruba Central, Mist, Ruckus and similar API connectors not found. Add vendor API pagination, rate limits and health diagnosis. **Evidence:** E07. | P1 / F |
| F42 **SD-WAN orchestrators and overlay** | Orchestrator/edge/uplink monitoring; NPM. [S14](https://documentation.solarwinds.com/en/success_center/npm/content/npm-velocloud-sdwan.htm) [S04](https://documentation.solarwinds.com/en/success_center/npm/content/release_notes/npm_2026-2_release_notes.htm) | Partial | FortiGate SNMP WAN health exists. No VeloCloud/Catalyst/Prisma/FortiManager orchestration integration or overlay inventory found. **Evidence:** E08. | P1 / F |
| F43 **Azure gateways and VPN connections** | Azure network monitoring; NPM. [S15](https://documentation.solarwinds.com/en/success_center/npm/content/npm-cloud-monitor-azure-gateways-and-site2site.htm) | Not found | No Azure gateway/site-to-site account integration found. NPM scope includes gateways/connections, not all cloud observability. **Evidence:** E07. | P2 / F |

### Troubleshooting and customization

| ID / feature | SolarWinds baseline / scope | Status | ZenPlus evidence and gap | Priority / workstream |
|---|---|---|---|---|
| F44 **Continuous hop-by-hop path monitoring** | NetPath; NPM. [S19](https://www.solarwinds.com/network-performance-monitor/use-cases/network-troubleshooting) | Observed | Live TCP SYN probe, multiple flows, latency/loss and branching path present. Retain and validate path semantics and target reachability. **Evidence:** E05. | P1 / B |
| F45 **Path history and route comparison** | Historical NetPath; NPM. [S19](https://www.solarwinds.com/network-performance-monitor/use-cases/network-troubleshooting) | Observed | History, routes, events and compare controls plus endpoints exist. A stale demo OK label is a fixture/freshness-policy question; reproduce on an enabled test probe before calling it a defect. **Evidence:** E05 C06. | P0 / A |
| F46 **Multiple network-path vantage points** | Distributed path probes; NPM. [S19](https://www.solarwinds.com/network-performance-monitor/use-cases/network-troubleshooting) [S26](https://documentation.solarwinds.com/en/success_center/orionplatform/content/orion_platform_scalability_engine_guidelines.htm?CMP=DIRECT&CMPSource=THW) | Unverified | Multiple-vantage NetPath execution was not verified. Offline development sensors do not establish lack of support; inspect capability wiring and test an isolated remote probe. **Evidence:** E05 E13 C06. | P1 / F |
| F47 **Shared cross-metric timeline** | PerfStack; NPM. [S19](https://www.solarwinds.com/network-performance-monitor/use-cases/network-troubleshooting) | Not found | Individual charts exist; no saved shared-timeline investigation across devices, interfaces, paths, alerts and applications located. **Evidence:** E02 E05 E14. | P1 / G |
| F48 **Custom OID scalar/table polling** | Universal Device Pollers; NPM. [S06](https://documentation.solarwinds.com/en/success_center/npm/content/core-monitoring-mibs-with-universal-device-pollers-sw548.htm) | Available / unproven | Template groups, counters, enums and scale are implemented. Validate arbitrary custom OIDs, row identity and reusable import/export workflows. **Evidence:** E07 C01. | P1 / B |
| F49 **MIB resolution and derived formulas** | MIB and poller customization; NPM. [S06](https://documentation.solarwinds.com/en/success_center/npm/content/core-monitoring-mibs-with-universal-device-pollers-sw548.htm) [S07](https://documentation.solarwinds.com/en/success_center/npm/content/core-transforming-poller-results-sw704.htm) | Partial | MIB upload is storage-only. No complete symbolic MIB browser/compile path or arbitrary multi-poller formula editor demonstrated. **Evidence:** E12 C01. | P1 / B |
| F50 **Network capacity forecasts** | Exhaustion-date prediction; NPM. [S20](https://documentation.solarwinds.com/en/success_center/npm/content/npm-capacity-forecasting-sw1526.htm) | Not found | Headroom and 95th percentile exist, but no CPU/memory/interface exhaustion forecast found. Add trend models and data-quality gates. **Evidence:** E17 C10. | P1 / G |
| F51 **Dynamic statistical thresholds** | Baseline thresholds; NPM. [S21](https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-orion-baseline-data-calculation.htm) | Not found | Static thresholds are present. Server software baselines are a different feature; network baseline learning was not found. **Evidence:** E09 C03. | P1 / G |
| F52 **Passive application/network timing** | Quality of Experience; NPM. [S24](https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-monitoring-quality-of-experience-sw3278.htm) [S25](https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-deploying-a-network-sensor-sw3239.htm) | Partial | On-demand connection capture exists in source. No continuous SPAN/server sensor pipeline showing network versus application response time established. **Evidence:** C13. | P2 / F |

### Alerting, logs and reporting

| ID / feature | SolarWinds baseline / scope | Status | ZenPlus evidence and gap | Priority / workstream |
|---|---|---|---|---|
| F53 **Threshold rules and recovery** | Advanced alerting; NPM. [S22](https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-customize-thresholds-per-object.htm) | Partial | Core scalar/interface rules exist; verify all supported metrics, clear conditions and mixed-engine behavior before operational sign-off. **Evidence:** E09 C03. | P0 / C |
| F54 **AND/OR compound conditions** | Multi-condition alerts; NPM. [S01](https://www.solarwinds.com/network-performance-monitor) | Partial | Wizard exposes AND/OR. Reviewed periodic network evaluator selects legacy metric fields and does not apply the conditions array. Deployment verification required. **Evidence:** E09 C03 C04. | P0 / C |
| F55 **Hold time and reset hysteresis** | Sustained thresholds; NPM. [S22](https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-customize-thresholds-per-object.htm) | Partial | Scalar sustained logic exists; template rules evaluate latest values from a lookback. Complete reset/hold semantics across every evaluator. **Evidence:** E09 C03. | P0 / C |
| F56 **Component-specific alert lifecycle** | Hardware/protocol alerts; NPM. [S13](https://documentation.solarwinds.com/en/success_center/npm/content/npm-load-balancers-eventsalertsreports.htm) [S27](https://documentation.solarwinds.com/en/success_center/npm/content/onboarding/npm-ob-beyond-getting-started.htm) | Partial | Canonical fan/PSU/VPN/HA/BGP keys are accepted but absent from periodic supported set. tpl_* provides an alternative, with device-level aggregation. **Evidence:** E07 E09 C03 C04. | P0 / C |
| F57 **Notifications and escalation** | Alert actions and escalation; NPM. [S27](https://documentation.solarwinds.com/en/success_center/npm/content/onboarding/npm-ob-beyond-getting-started.htm) | Available / unproven | Channel configuration, cooldowns and escalation controls exist. Zero destinations and disabled development gateways are expected configuration state. Test transport with local sinks before optional real delivery. **Evidence:** E09 E10. | Validation / A |
| F58 **Maintenance, snooze and dependencies** | Alert suppression; NPM. [S04](https://documentation.solarwinds.com/en/success_center/npm/content/release_notes/npm_2026-2_release_notes.htm) | Partial | Maintenance/snooze controls exist. Prove node-down, dependency, schedule and maintenance suppression consistently across metric and event engines. **Evidence:** E02 E09 C03 C05. | P0 / C |
| F59 **ITSM and collaboration workflows** | Platform integrations; Platform. [S27](https://documentation.solarwinds.com/en/success_center/npm/content/onboarding/npm-ob-beyond-getting-started.htm) | Partial | Webhook/Slack/Telegram cataloged ready; Teams and Jira/ServiceNow explicitly planned. Generic webhook does not establish bidirectional incident sync. **Evidence:** E10. | P2 / G |
| F60 **SNMP trap reception and history** | Trap ingestion; NPM. [S23](https://documentation.solarwinds.com/en/success_center/orionplatform/content/lm/la-orion-log-viewer.htm) | Available / unproven | v1/v2c receiver and feed exist; an empty development feed is not failure. v3 traps are explicitly deferred in source, independently of test traffic; verify required comparator scope before prioritizing. **Evidence:** E11 C08. | P1 / C |
| F61 **Network-device syslog workflow** | Log Viewer; NPM. [S23](https://documentation.solarwinds.com/en/success_center/orionplatform/content/lm/la-orion-log-viewer.htm) | Not found | No network syslog receiver/search/rule workflow found. Server logs and a Syslog port label do not establish ingestion. **Evidence:** E11 C08. | P1 / C |
| F62 **Alert/event history and acknowledgement** | Operational alert console; NPM. [S27](https://documentation.solarwinds.com/en/success_center/npm/content/onboarding/npm-ob-beyond-getting-started.htm) | Observed | Live events, active alerts and acknowledge controls visible. Alert storm, grouping, stale-state and operator workflow tests remain. **Evidence:** E01 E02 E09. | P1 / C |
| F63 **Availability/SLA and outage reports** | Availability reporting; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Available / unproven | Availability views, report templates and coverage disclosure exist. Short synthetic history is not a product gap; validate denominator, maintenance and unknown-data policy with fixtures, then a real-device pilot. **Evidence:** E14 E15. | Validation / A |
| F64 **Custom reports and scheduled export** | Report builder and schedules; NPM. [S02](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm) | Partial | Fixed-section reporting and export/schedule controls exist. Arbitrary object/field/query authoring was not found in the inspected builder. Empty schedules/history are development configuration, not missing capability. **Evidence:** E14. | P1 / G |

### Platform and enterprise readiness

| ID / feature | SolarWinds baseline / scope | Status | ZenPlus evidence and gap | Priority / workstream |
|---|---|---|---|---|
| F65 **Role and object access controls** | Roles and scoped access; Platform. [S27](https://documentation.solarwinds.com/en/success_center/npm/content/onboarding/npm-ob-beyond-getting-started.htm) | Available / unproven | Viewer tag scope and permission model visible. Read-only inspection cannot prove enforcement on API, reports, exports and nested objects. **Evidence:** E16 C12. | P1 / H |
| F66 **Directory authentication and SSO** | Enterprise authentication; Platform. [S27](https://documentation.solarwinds.com/en/success_center/npm/content/onboarding/npm-ob-beyond-getting-started.htm) | Partial | LDAP/RADIUS implementation exists; lack of configured development authentication is not a defect. SAML/OIDC support was not established; track this scope separately from integration testing. **Evidence:** E16 C12. | P2 / H |
| F67 **API and auditability** | Platform extensibility; Platform. [S27](https://documentation.solarwinds.com/en/success_center/npm/content/onboarding/npm-ob-beyond-getting-started.htm) | Partial | FastAPI and audit logging exist in source. Public integration contract, scoped API lifecycle and complete audit coverage need certification. **Evidence:** C04 C06 C12. | P2 / H |
| F68 **Distributed polling and buffering** | Remote collectors; Platform. [S26](https://documentation.solarwinds.com/en/success_center/orionplatform/content/orion_platform_scalability_engine_guidelines.htm?CMP=DIRECT&CMPSource=THW) | Available / unproven | Remote sensor deployment and assignments exist. An offline development sensor is not a distributed-polling defect; verify buffering, replay, timestamps and version compatibility in an isolated test. **Evidence:** E13. | Validation / A |
| F69 **Polling capacity and load distribution** | Additional polling engines; Add-on. [S26](https://documentation.solarwinds.com/en/success_center/orionplatform/content/orion_platform_scalability_engine_guidelines.htm?CMP=DIRECT&CMPSource=THW) | Unverified | 40-device mixed estate is not a scale benchmark. No measured sustainable element capacity, engine balancing or saturation behavior established. **Evidence:** E01 E13. | P1 / H |
| F70 **Monitoring-system high availability** | HA deployment; Add-on. [S04](https://documentation.solarwinds.com/en/success_center/npm/content/release_notes/npm_2026-2_release_notes.htm) [S27](https://documentation.solarwinds.com/en/success_center/npm/content/onboarding/npm-ob-beyond-getting-started.htm) | Not found | No controller/collector failover pools or proven database failover found. Device HA panels monitor equipment; they do not protect ZenPlus itself. **Evidence:** E13. | P1 / H |
| F71 **Retention, backup and restore** | Operational continuity; Platform. [S26](https://documentation.solarwinds.com/en/success_center/orionplatform/content/orion_platform_scalability_engine_guidelines.htm?CMP=DIRECT&CMPSource=THW) | Unverified | Settings exist in navigation; historical continuity, restore integrity and disaster recovery were not exercised. Define and prove RPO/RTO. **Evidence:** E14 E15. | P1 / H |

## Module boundaries

| Capability | SolarWinds module | Assessment treatment |
|---|---|---|
| NetFlow traffic analytics | NTA | ZenPlus has NetFlow overview, forensics, anomalies and capacity screens; the inspected windows contained no flows. Treat as an adjacent capability, not proof of NPM depth or NTA parity. |
| Configuration backup/compliance/firmware | NCM | Config Backup is present in ZenPlus navigation. A backup menu does not establish configuration compliance, approval, vulnerability, rollback or firmware-upgrade parity. |
| Endpoint/MAC/user/switch-port tracking | UDT | ZenPlus has an extensive UDT navigation surface and related collector code. A full UDT assessment was outside this NPM review. |
| IP address and DHCP/DNS management | IPAM | Do not mark IPAM absence as an NPM deficiency. Assess separately if network-management-suite replacement is intended. |
| VoIP quality and IP SLA operations | VNQM | Do not count full voice/call-manager/IP-SLA coverage as an NPM requirement without expanding scope. |
| Deep server/application/virtualization observability | SAM / VMAN / other | ZenPlus APM and server modules extend beyond this review. They do not compensate for missing network-device functions. |
| Multiple-instance enterprise console | EOC | Separate federation requirement; a single controller plus remote sensors is not evidence of a multi-instance enterprise console. |

Distinct module scope: [[S28](https://documentation.solarwinds.com/en/success_center/nam/content/nam_administrator_guide.htm) [S29](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm_administrator_guide.htm?CMP=DIRECT&CMPSource=THW)]. Fleet Routing Insights: [[S17](https://documentation.solarwinds.com/en/success_center/npm/content/npm-routing-insights-home.htm)]. HA/scalability: [[S26](https://documentation.solarwinds.com/en/success_center/orionplatform/content/orion_platform_scalability_engine_guidelines.htm?CMP=DIRECT&CMPSource=THW)]. RF heat maps and Network Atlas caveats: [[S09](https://documentation.solarwinds.com/en/success_center/npm/content/core-adding-wireless-access-points-sw3344.htm) [S04](https://documentation.solarwinds.com/en/success_center/npm/content/release_notes/npm_2026-2_release_notes.htm)].

## Implementation plan

Revision 2 separates development work from real-device certification. Weeks 1-2 establish deterministic fixtures and reproduce source candidates; there is no instruction to repair an idle development sensor or configure production channels as a product fix.

Previous estimates of 80-129 person-weeks, 8-12 weeks for qualified core scope and 20-28 weeks for broad enterprise scope are provisional scenarios, not validated revised estimates. Workstream A has changed scope. Re-estimate after fixture-based triage; untested behavior must not automatically become implementation work.

The earlier calendar scenario assumed about five effective delivery engineers plus part-time product/network ownership and timely access to equipment/APIs. Missing access leaves real-device certification unscheduled; the development calendar must not imply that certification has already been possible.

Use two tracks: implement source/UI-backed missing capabilities and reproduce candidate defects now; run model/firmware, delivery, multi-site, soak and recovery qualification when appropriate hardware, vendor virtual appliances and external systems are available.

The later real-device pilot may target 20 representative nodes across two sites, 100 interfaces and 14 days of evidence, followed by scale/soak testing. These are proposed acceptance targets to agree, not prerequisites for this development review or observed current deficiencies.

Keep NPM requirements, shared platform functions and separately licensed modules distinct. Prioritize target device families, versioned schemas and reversible releases. Confirm numerical accuracy and vendor compatibility separately from simulator-based semantic correctness.

### A - Build development validation and confirm findings

**Window:** Weeks 1-2. **Effort:** 4-6 person-weeks. **Owner:** SRE + backend + QA.

**Dependencies:** None

**Deliver:** Map deployed build to source; prepare deterministic SNMP/counter fixtures, a controlled test clock, local notification sinks and remote-probe fixtures. Reproduce alert candidates and agree health/freshness semantics. Catalogue implemented, missing and unverified behavior; size confirmed defects only.

**Acceptance:** Repeatable scenarios cover known counter rates/resets, missing data, compound alerts, hold/recovery and simulated component faults. Results distinguish fixture behavior from product defects. No physical network equipment or production destination is required for this stage.

**Primary feature rows:** F01, F04, F07, F09, F12, F18, F45, F57, F63, F68

### B - Harden discovery and polling contracts

**Window:** Weeks 2-6. **Effort:** 6-9 person-weeks. **Owner:** Collector + backend + QA.

**Dependencies:** A: fixtures and representative model/firmware test plan

**Deliver:** Verify discovery/polling contracts with synthetic records; schedule real-device SNMPv3 and IPv6 interoperability separately. Test selection/reindex, wrap/reboot/discontinuity and freshness behavior. Implement missing MIB resolve/compile and custom-poller reuse workflows after checking existing depth.

**Acceptance:** Discover a controlled subnet twice without duplicate identities. Preserve intended monitoring after ifIndex changes. Certify v2c/v3, 1/10/100-Gbps counters as applicable, resets and missing samples against device/traffic-generator reference. Custom scalar/table and transformed metric are usable in charts and alerts.

**Primary feature rows:** F02, F03, F05, F06, F08, F10, F11, F13, F14, F15, F21, F44, F48, F49

### C - Unify alert semantics and network events

**Window:** Weeks 1-6. **Effort:** 8-12 person-weeks. **Owner:** Backend lead + collector + QA.

**Dependencies:** A fixtures; code tracing and unit-level reproduction can start immediately

**Deliver:** Confirm the source candidates before changing code, then correct reproduced AND/OR, hold/reset, dependency and component-identity defects. Add capability validation, syslog workflow and required trap support. Exercise transports with local sinks during development.

**Acceptance:** Controlled tests cover supported metric families, parent-child suppression, independent tunnel state, hold/reset and replay idempotence. Synthetic traps/syslog reach local notification sinks. Real-device protocol and external-channel certification remains a later gate.

**Primary feature rows:** F24, F53, F54, F55, F56, F58, F60, F61, F62

### D - Complete topology and protocol workflows

**Window:** Weeks 5-10. **Effort:** 8-12 person-weeks. **Owner:** Collector + UI + QA.

**Dependencies:** B identity/counters; C object alerting

**Deliver:** Discovered topology with aging and change history; explicit dependencies; BGP/OSPF route/neighbor detail; STP/LACP/VRRP relationships; duplex diagnostics and stack depth. Stage multicast and physical device views by demand.

**Acceptance:** Compare a two-site reference topology with LLDP/CDP and device CLI. Link removals age correctly; routed next hops and neighbors map correctly. Simulated peer/stack/link failures show impact and suppress child noise. Multicast/VRF extensions have separate acceptance scope.

**Primary feature rows:** F16, F17, F19, F20, F22, F23, F25, F26, F27, F28, F29

### E - Certify vendor insight and wireless depth

**Window:** Weeks 7-16. **Effort:** 14-22 person-weeks. **Owner:** Two collector/integration engineers + UI + QA.

**Dependencies:** B custom metrics; C component alerts; D relationships

**Deliver:** Prioritize installed device families. Add Nexus vPC, complete ASA/PAN-OS tunnels, F5 pool members/relationships and optional GTM/iControl; implement Cisco WLC plus wireless clients/SSID/radio history. Expand hardware/stack coverage with a model/firmware matrix.

**Acceptance:** For each supported family use at least two representative firmware/model combinations where feasible, plus a documented negative/unsupported case. Compare counters and states with vendor output. Prove component failure/recovery and no stale health. Wireless client identity and RF metrics match controller data.

**Primary feature rows:** F30, F31, F33, F34, F35, F36, F37, F38, F39, F40

### F - Add hybrid and distributed diagnostics

**Window:** Weeks 11-22. **Effort:** 12-20 person-weeks. **Owner:** Integration + collector + UI + QA.

**Dependencies:** B/C/D; actual controller/API access

**Deliver:** Select one SD-WAN and one cloud-wireless provider first; then expand. Add Azure gateway integration and ACI if deployed. Certify remote NetPath. Design continuous packet-based QoE as a separately sized workstream.

**Acceptance:** Controller entities reconcile without duplicates under pagination, credential expiry and API throttling. Tunnel uplink metrics match vendor console. Same target measured from two sites retains correct source identity. QoE, if selected, separates network and application timing under controlled induced latency.

**Primary feature rows:** F32, F41, F42, F43, F46, F52

### G - Forecasting, investigations and reporting

**Window:** Weeks 7-14. **Effort:** 8-12 person-weeks. **Owner:** Backend/data + UI + QA.

**Dependencies:** A synthetic history; B metric metadata; C events; real history for later forecast qualification

**Deliver:** Shared time-aligned investigation workspace; saved/shareable read-only views; baseline thresholds; interface/CPU/memory forecasts; richer object-field reports; scheduled delivery. ITSM integration is optional scope.

**Acceptance:** Overlay interface, device, path and alert data at one timestamp. Forecasts use peak/average trends, require sufficient history and suppress misleading dates on flat/noisy/sparse data. Backtest on held-out periods. Report and exported data match source queries and preserve access limits.

**Primary feature rows:** F47, F50, F51, F59, F64

### H - Enterprise reliability and release gates

**Window:** Weeks 9-24; certification buffer to week 28. **Effort:** 10-16 person-weeks. **Owner:** SRE + platform/backend + QA/security.

**Dependencies:** A-C verified; agreed target scale and later test infrastructure

**Deliver:** Define supported element capacity; load and soak testing; HA/DR architecture and deployment automation; collector reassignment; backup/restore; RBAC/API/export isolation; authenticated integration documentation.

**Acceptance:** Run 72-hour load tests at agreed target scale and a 30-day pilot. Proposed starting target: 1,000 nodes / 10,000 interfaces, validated at chosen polling intervals rather than claimed in advance. Demonstrate controller failover with no duplicate actions, RTO <=5 min and RPO <=1 min if architecture supports them; otherwise publish measured limits.

**Primary feature rows:** F65, F66, F67, F69, F70, F71

## Acceptance tests

These tests were not executed during the read-only assessment.

| ID / test | Required evidence | Workstream |
|---|---|---|
| T01 Polling correctness | Compare expected polls with persisted timestamps; v2c/v3, timeouts and IPv6; prove no-data differs from zero or down. | A/B |
| T02 Traffic accuracy | Known traffic at several rates; counter wrap, restart, link-speed change and ifIndex change; direction-specific utilization. | B |
| T03 Discovery lifecycle | Repeated scan, new interface, removed device, changed IP/hostname, duplicates, exclusions and import audit. | B |
| T04 Alert semantic contract | AND/OR truth tables, hold, reset, recovery, maintenance, schedules and all canonical/template state metrics. | C |
| T05 Fault impact and notification | Parent failure suppresses children; independently failing tunnels; message retries and replay; acknowledgement stops escalation. | A/C/D |
| T06 Vendor correctness | Model/firmware matrix with real reference output; unsupported OIDs explicit; sensor alarms propagate to summary. | D/E |
| T07 Wireless and controller lifecycle | Roaming/disconnect, client/SSID/radio association, API pagination/rate limits/credential expiry, stale children. | E/F |
| T08 Path correctness | Multi-flow branch behavior, target timeout versus intermediate ICMP silence, history comparison, stale expiry and source identity. | B/F |
| T09 Analytics and reports | Coverage-aware SLA, forecast backtesting, synchronized timeline, exported values and scheduled-channel delivery. | G |
| T10 Enterprise recovery and access | 72-hour scale test, 30-day soak, collector outage replay, controller/database failure, restore and cross-role API/export isolation. | H |

## Release gates

1. **Development validation:** deterministic fixtures reproduce or dismiss source candidates; alert semantics, counters, missing data and local notification sinks pass.

2. **Real-device qualification, when equipment is available:** required device families, known traffic, faults, delivery and topology match reference outputs; no confirmed critical defect remains.

3. **Enterprise:** required integrations, analytics, scale, access and recovery objectives pass a sustained pilot.

4. **Full parity claim:** every required baseline capability has reproducible evidence and all exclusions/entitlements are explicit.

## Week-1 decisions

- Which vendors, models and firmware are launch requirements, and which can be tested now using hardware, vendor virtual appliances or simulators?
- What are the target nodes, interfaces, wireless clients, traps/syslogs per second, polling intervals and retention? Device count alone is insufficient for sizing.
- Is the commercial target standalone NPM replacement or the broader NAM/Observability suite? Keep adjacent modules and licensed extensions in separate acceptance scopes.
- Which fixtures, mock notification sinks and test accounts can support development validation, and when can a separate real-device pilot begin?

## Appliance evidence register

These are time-of-inspection notes. Live links may change; they are not immutable screenshots.

### E01 - Dashboard and inventory

[Appliance view](https://192.168.8.221/)

40 monitored devices; the snapshot mixed lab, QA, private-network and Internet targets. Live alert totals changed during inspection. Do not interpret these totals as a production incident census. Development context: this snapshot is not evidence of a production failure or missing functionality; configuration, fixture and implementation findings are separated in Revision 2.

### E02 - Cisco device detail

[Appliance view](https://192.168.8.221/devices/e531e171-6f71-4978-bee7-d6991276dee6)

cisco-lab-01, 127.0.0.15, SNMP v2c on port 16161. CPU, memory, temperature, fans, PSU, stack and PoE visible. Fan warning coexisted with a 100/100 score. One-hour availability initially covered 15% of the range. Development context: this snapshot is not evidence of a production failure or missing functionality; configuration, fixture and implementation findings are separated in Revision 2.

### E03 - Interfaces and link utilization

[Appliance view](https://192.168.8.221/link-utilization)

47 monitored links, zero with NetFlow; displayed throughput was zero/dashes and fleet utilization 0%. Filters, errors, discards, flaps, drill-down and manual interface capacity controls were visible. Meaningful loaded-link measurements were not demonstrated. Development context: this snapshot is not evidence of a production failure or missing functionality; configuration, fixture and implementation findings are separated in Revision 2.

### E04 - Discovery

[Appliance view](https://192.168.8.221/discovery)

No discovery profiles; no import history in /discovery/imports. Wizard exposes scope, credentials/protocols, schedule, classification/rules and review. No scan, import or saved draft was started. Development context: this snapshot is not evidence of a production failure or missing functionality; configuration, fixture and implementation findings are separated in Revision 2.

### E05 - NetPath list and live detail

[Appliance view](https://192.168.8.221/netpath/probes/448e132d-ceaf-4ea3-b461-b5b10daee19c)

Five probes; a live HTTPS path had TCP SYN, four flows, history, branching hops, ASN ownership, routes, events and comparison controls. A separately labeled demo probe retained an OK label with a last check 28 days ago. Development context: this snapshot is not evidence of a production failure or missing functionality; configuration, fixture and implementation findings are separated in Revision 2.

### E06 - Network Studio

[Appliance view](https://192.168.8.221/maps/manual)

Selected Traffic lab (QA) map described scratch simulator devices: seven devices, eight links, three shapes. Manual design, live mode, NOC fullscreen, interface labels and LLDP/CDP quick-connect control were present; automatic lifecycle maintenance was not demonstrated.

### E07 - Monitoring templates

[Appliance view](https://192.168.8.221/settings/general?tab=templates)

10 built-in packs, 222 metrics, eight attached devices. Packs: Aruba CX, Aruba controller, Cisco ASA, Cisco IOS/XE, Dell OS10, F5, FortiGate, JunOS, MikroTik and PAN-OS. ASA, CX, Dell and MikroTik had zero attached devices. Development context: this snapshot is not evidence of a production failure or missing functionality; configuration, fixture and implementation findings are separated in Revision 2.

### E08 - FortiGate detail

[Appliance view](https://192.168.8.221/devices/f85c4b75-a2f8-42d7-90a4-ce2ef4a33a4d)

fgt-lab-01, 127.0.0.14, SNMP v2c on port 16161. VDOM, HA, VPN, SD-WAN, FortiAP and FortiSwitch panels populated. A 100/100 score coexisted with an unsynchronized HA member, down tunnel, dead WAN check and PSU alarm. Development context: this snapshot is not evidence of a production failure or missing functionality; configuration, fixture and implementation findings are separated in Revision 2.

### E09 - Alert definitions and wizard

[Appliance view](https://192.168.8.221/alert-rules)

Existing ICMP, SNMP interface/CPU/memory/reboot and other rules. Nine-step wizard exposes AND/OR conditions, hold duration, scope, reset, trigger actions, escalation and schedules. Inspected without saving or sending tests.

### E10 - Notification channels

[Appliance view](https://192.168.8.221/channels)

Zero configured channels; two default SMTP/SMS gateways disabled; alert routing not set. Catalog labels Email, SMS, Webhook, Slack and Telegram ready; Teams and Jira/ServiceNow planned. Development context: this snapshot is not evidence of a production failure or missing functionality; configuration, fixture and implementation findings are separated in Revision 2.

### E11 - Trap receiver UI

[Appliance view](https://192.168.8.221/traps)

UDP/162 feed with time/severity filters; zero traps in selected one-hour window. This is an empty observation, not proof of receiver failure. Development context: this snapshot is not evidence of a production failure or missing functionality; configuration, fixture and implementation findings are separated in Revision 2.

### E12 - MIB library

[Appliance view](https://192.168.8.221/settings/general?tab=mibs)

Zero installed MIBs. UI explicitly states uploaded files are stored on disk and runtime compilation will arrive in a later update.

### E13 - Remote sensor fleet

[Appliance view](https://192.168.8.221/settings/general?tab=sensors)

One VMware sensor, v1.23.5, seven assignments, heartbeat approximately two days old, offline. OVA/OVF/QCOW2/VHDX-related deployment artifacts available. Offline sensor cannot demonstrate remote polling continuity. Development context: this snapshot is not evidence of a production failure or missing functionality; configuration, fixture and implementation findings are separated in Revision 2.

### E14 - Reports

[Appliance view](https://192.168.8.221/reports)

Library includes availability, performance, inventory, alerts and headroom reports. Custom builder assembles fixed sections. No generated report history and no schedules in /reports/schedules. PDF/HTML and some Excel/CSV export controls visible; exports and delivery were not executed. Development context: this snapshot is not evidence of a production failure or missing functionality; configuration, fixture and implementation findings are separated in Revision 2.

### E15 - Availability

[Appliance view](https://192.168.8.221/availability)

One-hour fleet view explicitly said measured over 14 minutes. 54.65% availability was a transient mixed-estate sample, not a full-hour production SLA result. Coverage disclosure exists but operational acceptance remains unproven. Development context: this snapshot is not evidence of a production failure or missing functionality; configuration, fixture and implementation findings are separated in Revision 2.

### E16 - Access controls

[Appliance view](https://192.168.8.221/settings/general?tab=users)

Administrator and viewer roles visible; viewer visibility limited to a tag. Roles/permissions and authentication tabs present. No separate-account isolation or login test was performed. Authentication tab explicitly showed LDAP/AD and RADIUS not configured; only local sign-in was active.

### E17 - Capacity view

[Appliance view](https://192.168.8.221/netflow/capacity)

95th-percentile utilization compared with interface speed. No traffic in selected window. This is a headroom/billing view, not an exhaustion-date forecast. Development context: this snapshot is not evidence of a production failure or missing functionality; configuration, fixture and implementation findings are separated in Revision 2.

## Source corroboration

Local HEAD b4256ff; source/deployed identity was not verified.

### C01 - SNMP collector and counter handling

[poller/internal/checker/snmp/collector.go](<C:/Users/user/Documents/ZenPlus/poller/internal/checker/snmp/collector.go>)

High-capacity interface OIDs at lines 826-829; rate wrap/reset logic around 1094. Vendor collection and standard polling exist. No loaded-link accuracy test executed.

### C02 - Standard network tables

[poller/internal/checker/snmp/standard_network_groups.go](<C:/Users/user/Documents/ZenPlus/poller/internal/checker/snmp/standard_network_groups.go>)

BGP at line 23, OSPF 33, VRRP/VRRPv3 42/53, STP 65/75, LACP 85. These are collector capabilities, not proof of a complete routing dashboard.

### C03 - Network alert evaluator

[server/app/services/network_alert_service.py](<C:/Users/user/Documents/ZenPlus/server/app/services/network_alert_service.py>)

Supported canonical metric set at 43-55; template evaluation 204-246; selection/evaluation loop 468-566; apply 569-613. Compound fields are fetched but not used in the reviewed evaluation branches. Template conditions use latest values, not continuous-duration proof.

### C04 - Rule API contract

[server/app/api/v1/alert_rules.py](<C:/Users/user/Documents/ZenPlus/server/app/api/v1/alert_rules.py>)

Canonical fan/PSU/VPN/HA/BGP keys accepted at 28-30; conditions mirror the first condition to legacy fields at 386-394. Compare against C03 before claiming evaluator parity.

### C05 - Dependency suppression

[server/app/api/v1/alert_engine.py](<C:/Users/user/Documents/ZenPlus/server/app/api/v1/alert_engine.py>)

Upstream lookup at 381; suppression in event-driven paths around 707-843 and 1181-1278. This is not evidence of equivalent suppression in periodic SNMP evaluation.

### C06 - NetPath collector / API

[poller/internal/checker/netpath/prober.go](<C:/Users/user/Documents/ZenPlus/poller/internal/checker/netpath/prober.go>)

IPv4-only rejection at 109. Related API server/app/api/v1/netpath.py exposes snapshots, graph, hops, routes, events and comparison. Remote probe parity not demonstrated.

### C07 - Discovery schema

[server/app/schemas/discovery_v2.py](<C:/Users/user/Documents/ZenPlus/server/app/schemas/discovery_v2.py>)

Scope/import/schedule types 12-16; protocols and credential references 33-38; classifications and limits 39-56. Local implementation corroborates empty live workflows.

### C08 - Trap protocol boundary

[poller/internal/checker/snmp/traps.go](<C:/Users/user/Documents/ZenPlus/poller/internal/checker/snmp/traps.go>)

Lines 39-40 explicitly defer v3 traps; current listener is v1/v2c. SNMPv3 polling and SNMPv3 trap reception are separate capabilities.

### C09 - Map assistance

[server/app/api/v1/manual_maps.py](<C:/Users/user/Documents/ZenPlus/server/app/api/v1/manual_maps.py>)

LLDP/CDP link assistance begins at 603; uses topology_links around 654. Existing canvas links and auto-discovered topology must not be treated as identical lifecycle behavior.

### C10 - Reporting capacity sections

[server/app/services/report_sections.py](<C:/Users/user/Documents/ZenPlus/server/app/services/report_sections.py>)

Capacity sections around 1209-1250 calculate present filesystem/link headroom. Template at 1531-1535 selects these sections. No forecast implementation found in scoped searches.

### C11 - Vendor coverage migrations

[scripts/migrate-080-snmp-network-device-coverage.sql](<C:/Users/user/Documents/ZenPlus/scripts/migrate-080-snmp-network-device-coverage.sql>)

Adds CX/OS10 and extends Cisco, FortiGate, PAN-OS and MikroTik. Earlier 062/063 migrations define the main packs. Presence in source does not certify every device/firmware combination.

### C12 - Authentication and role foundation

[server/app/services/external_auth.py](<C:/Users/user/Documents/ZenPlus/server/app/services/external_auth.py>)

LDAP/RADIUS and group-to-role mapping implemented; server/app/models/role.py stores permission lists. SAML/OIDC and enforcement coverage were not established.

### C13 - Capture foundation

[server/app/services/network_capture_service.py](<C:/Users/user/Documents/ZenPlus/server/app/services/network_capture_service.py>)

Bounded on-demand capture lifecycle; agent implementation under ZenPlus_Agent/internal/netcapture. This does not establish continuous SPAN-based application/network response-time analytics.

## Official references

Consulted 10 September 2026.

- **S01:** [NPM product capabilities](https://www.solarwinds.com/network-performance-monitor)
- **S02:** [NPM administrator guide and feature index](https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm)
- **S03:** [NPM documentation / current release index](https://documentation.solarwinds.com/en/success_center/npm/content/npm_documentation.htm?id=15032385540)
- **S04:** [NPM 2026.2 release notes](https://documentation.solarwinds.com/en/success_center/npm/content/release_notes/npm_2026-2_release_notes.htm)
- **S05:** [Polling methods](https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-choosing-your-polling-method-sw1223.htm?cshid=OrionCoreAG_ChoosingPollingMethods)
- **S06:** [Universal Device Pollers](https://documentation.solarwinds.com/en/success_center/npm/content/core-monitoring-mibs-with-universal-device-pollers-sw548.htm)
- **S07:** [Transform poller results](https://documentation.solarwinds.com/en/success_center/npm/content/core-transforming-poller-results-sw704.htm)
- **S08:** [Wireless infrastructure and supported controller families](https://documentation.solarwinds.com/en/success_center/npm/content/core-monitoring-wireless-networks-sw764.htm)
- **S09:** [Wireless heat-map requirements](https://documentation.solarwinds.com/en/success_center/npm/content/core-adding-wireless-access-points-sw3344.htm)
- **S10:** [Aruba Central API monitoring](https://documentation.solarwinds.com/en/success_center/npm/content/npm-aruba-central-wireless.htm)
- **S11:** [Nexus monitoring requirements](https://documentation.solarwinds.com/en/success_center/npm/content/npm-nexus-set-up-monitoring.htm)
- **S12:** [F5 Network Insight requirements](https://documentation.solarwinds.com/en/success_center/npm/content/npm-monitor-load-balancers.htm)
- **S13:** [F5 events, alerts and reports](https://documentation.solarwinds.com/en/success_center/npm/content/npm-load-balancers-eventsalertsreports.htm)
- **S14:** [VeloCloud SD-WAN monitoring](https://documentation.solarwinds.com/en/success_center/npm/content/npm-velocloud-sdwan.htm)
- **S15:** [Azure gateway and site-to-site monitoring](https://documentation.solarwinds.com/en/success_center/npm/content/npm-cloud-monitor-azure-gateways-and-site2site.htm)
- **S16:** [Multicast monitoring](https://documentation.solarwinds.com/en/success_center/npm/content/monitor-multicast-traffic.htm)
- **S17:** [Routing Insights and its license boundary](https://documentation.solarwinds.com/en/success_center/npm/content/npm-routing-insights-home.htm)
- **S18:** [Routing neighbor monitoring and its license boundary](https://documentation.solarwinds.com/en/success_center/npm/content/npm-routing-neighbors.htm)
- **S19:** [NPM NetPath and PerfStack troubleshooting](https://www.solarwinds.com/network-performance-monitor/use-cases/network-troubleshooting)
- **S20:** [Capacity forecasting](https://documentation.solarwinds.com/en/success_center/npm/content/npm-capacity-forecasting-sw1526.htm)
- **S21:** [Dynamic baselines](https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-orion-baseline-data-calculation.htm)
- **S22:** [Per-object and sustained thresholds](https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-customize-thresholds-per-object.htm)
- **S23:** [Log Viewer included with NPM](https://documentation.solarwinds.com/en/success_center/orionplatform/content/lm/la-orion-log-viewer.htm)
- **S24:** [Quality of Experience](https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-monitoring-quality-of-experience-sw3278.htm)
- **S25:** [SPAN / mirror-port packet analysis sensors](https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-deploying-a-network-sensor-sw3239.htm)
- **S26:** [Scalability engine guidelines](https://documentation.solarwinds.com/en/success_center/orionplatform/content/orion_platform_scalability_engine_guidelines.htm?CMP=DIRECT&CMPSource=THW)
- **S27:** [NPM advanced capabilities and shared platform](https://documentation.solarwinds.com/en/success_center/npm/content/onboarding/npm-ob-beyond-getting-started.htm)
- **S28:** [Network Automation Manager module boundaries](https://documentation.solarwinds.com/en/success_center/nam/content/nam_administrator_guide.htm)
- **S29:** [Network Configuration Manager scope](https://documentation.solarwinds.com/en/success_center/ncm/content/ncm_administrator_guide.htm?CMP=DIRECT&CMPSource=THW)