from collections import Counter

TITLE = 'ZenPlus vs SolarWinds NPM'
SUBTITLE = 'Network device monitoring: feature assessment and delivery plan'
DATE = '10 September 2026'
SCOPE = 'ZenPlus v1.23.10 | https://192.168.8.221/ | Read-only assessment'

# Baseline references. Labels in the matrix are intentionally short; the report
# includes clickable references with the exact official documentation URLs.
SOURCES = {
'S01': ('NPM product capabilities', 'https://www.solarwinds.com/network-performance-monitor'),
'S02': ('NPM administrator guide and feature index', 'https://documentation.solarwinds.com/en/success_center/npm/content/npm_administrator_guide.htm'),
'S03': ('NPM documentation / current release index', 'https://documentation.solarwinds.com/en/success_center/npm/content/npm_documentation.htm?id=15032385540'),
'S04': ('NPM 2026.2 release notes', 'https://documentation.solarwinds.com/en/success_center/npm/content/release_notes/npm_2026-2_release_notes.htm'),
'S05': ('Polling methods', 'https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-choosing-your-polling-method-sw1223.htm?cshid=OrionCoreAG_ChoosingPollingMethods'),
'S06': ('Universal Device Pollers', 'https://documentation.solarwinds.com/en/success_center/npm/content/core-monitoring-mibs-with-universal-device-pollers-sw548.htm'),
'S07': ('Transform poller results', 'https://documentation.solarwinds.com/en/success_center/npm/content/core-transforming-poller-results-sw704.htm'),
'S08': ('Wireless infrastructure and supported controller families', 'https://documentation.solarwinds.com/en/success_center/npm/content/core-monitoring-wireless-networks-sw764.htm'),
'S09': ('Wireless heat-map requirements', 'https://documentation.solarwinds.com/en/success_center/npm/content/core-adding-wireless-access-points-sw3344.htm'),
'S10': ('Aruba Central API monitoring', 'https://documentation.solarwinds.com/en/success_center/npm/content/npm-aruba-central-wireless.htm'),
'S11': ('Nexus monitoring requirements', 'https://documentation.solarwinds.com/en/success_center/npm/content/npm-nexus-set-up-monitoring.htm'),
'S12': ('F5 Network Insight requirements', 'https://documentation.solarwinds.com/en/success_center/npm/content/npm-monitor-load-balancers.htm'),
'S13': ('F5 events, alerts and reports', 'https://documentation.solarwinds.com/en/success_center/npm/content/npm-load-balancers-eventsalertsreports.htm'),
'S14': ('VeloCloud SD-WAN monitoring', 'https://documentation.solarwinds.com/en/success_center/npm/content/npm-velocloud-sdwan.htm'),
'S15': ('Azure gateway and site-to-site monitoring', 'https://documentation.solarwinds.com/en/success_center/npm/content/npm-cloud-monitor-azure-gateways-and-site2site.htm'),
'S16': ('Multicast monitoring', 'https://documentation.solarwinds.com/en/success_center/npm/content/monitor-multicast-traffic.htm'),
'S17': ('Routing Insights and its license boundary', 'https://documentation.solarwinds.com/en/success_center/npm/content/npm-routing-insights-home.htm'),
'S18': ('Routing neighbor monitoring and its license boundary', 'https://documentation.solarwinds.com/en/success_center/npm/content/npm-routing-neighbors.htm'),
'S19': ('NPM NetPath and PerfStack troubleshooting', 'https://www.solarwinds.com/network-performance-monitor/use-cases/network-troubleshooting'),
'S20': ('Capacity forecasting', 'https://documentation.solarwinds.com/en/success_center/npm/content/npm-capacity-forecasting-sw1526.htm'),
'S21': ('Dynamic baselines', 'https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-orion-baseline-data-calculation.htm'),
'S22': ('Per-object and sustained thresholds', 'https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-customize-thresholds-per-object.htm'),
'S23': ('Log Viewer included with NPM', 'https://documentation.solarwinds.com/en/success_center/orionplatform/content/lm/la-orion-log-viewer.htm'),
'S24': ('Quality of Experience', 'https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-monitoring-quality-of-experience-sw3278.htm'),
'S25': ('SPAN / mirror-port packet analysis sensors', 'https://documentation.solarwinds.com/en/success_center/orionplatform/content/core-deploying-a-network-sensor-sw3239.htm'),
'S26': ('Scalability engine guidelines', 'https://documentation.solarwinds.com/en/success_center/orionplatform/content/orion_platform_scalability_engine_guidelines.htm?CMP=DIRECT&CMPSource=THW'),
'S27': ('NPM advanced capabilities and shared platform', 'https://documentation.solarwinds.com/en/success_center/npm/content/onboarding/npm-ob-beyond-getting-started.htm'),
'S28': ('Network Automation Manager module boundaries', 'https://documentation.solarwinds.com/en/success_center/nam/content/nam_administrator_guide.htm'),
'S29': ('Network Configuration Manager scope', 'https://documentation.solarwinds.com/en/success_center/ncm/content/ncm_administrator_guide.htm?CMP=DIRECT&CMPSource=THW'),
}

EVIDENCE = {
'E01': ('Dashboard and inventory', '/', '40 monitored devices; the snapshot mixed lab, QA, private-network and Internet targets. Live alert totals changed during inspection. Do not interpret these totals as a production incident census.'),
'E02': ('Cisco device detail', '/devices/e531e171-6f71-4978-bee7-d6991276dee6', 'cisco-lab-01, 127.0.0.15, SNMP v2c on port 16161. CPU, memory, temperature, fans, PSU, stack and PoE visible. Fan warning coexisted with a 100/100 score. One-hour availability initially covered 15% of the range.'),
'E03': ('Interfaces and link utilization', '/link-utilization', '47 monitored links, zero with NetFlow; displayed throughput was zero/dashes and fleet utilization 0%. Filters, errors, discards, flaps, drill-down and manual interface capacity controls were visible. Meaningful loaded-link measurements were not demonstrated.'),
'E04': ('Discovery', '/discovery', 'No discovery profiles; no import history in /discovery/imports. Wizard exposes scope, credentials/protocols, schedule, classification/rules and review. No scan, import or saved draft was started.'),
'E05': ('NetPath list and live detail', '/netpath/probes/448e132d-ceaf-4ea3-b461-b5b10daee19c', 'Five probes; a live HTTPS path had TCP SYN, four flows, history, branching hops, ASN ownership, routes, events and comparison controls. A separately labeled demo probe retained an OK label with a last check 28 days ago.'),
'E06': ('Network Studio', '/maps/manual', 'Selected Traffic lab (QA) map described scratch simulator devices: seven devices, eight links, three shapes. Manual design, live mode, NOC fullscreen, interface labels and LLDP/CDP quick-connect control were present; automatic lifecycle maintenance was not demonstrated.'),
'E07': ('Monitoring templates', '/settings/general?tab=templates', '10 built-in packs, 222 metrics, eight attached devices. Packs: Aruba CX, Aruba controller, Cisco ASA, Cisco IOS/XE, Dell OS10, F5, FortiGate, JunOS, MikroTik and PAN-OS. ASA, CX, Dell and MikroTik had zero attached devices.'),
'E08': ('FortiGate detail', '/devices/f85c4b75-a2f8-42d7-90a4-ce2ef4a33a4d', 'fgt-lab-01, 127.0.0.14, SNMP v2c on port 16161. VDOM, HA, VPN, SD-WAN, FortiAP and FortiSwitch panels populated. A 100/100 score coexisted with an unsynchronized HA member, down tunnel, dead WAN check and PSU alarm.'),
'E09': ('Alert definitions and wizard', '/alert-rules', 'Existing ICMP, SNMP interface/CPU/memory/reboot and other rules. Nine-step wizard exposes AND/OR conditions, hold duration, scope, reset, trigger actions, escalation and schedules. Inspected without saving or sending tests.'),
'E10': ('Notification channels', '/channels', 'Zero configured channels; two default SMTP/SMS gateways disabled; alert routing not set. Catalog labels Email, SMS, Webhook, Slack and Telegram ready; Teams and Jira/ServiceNow planned.'),
'E11': ('Trap receiver UI', '/traps', 'UDP/162 feed with time/severity filters; zero traps in selected one-hour window. This is an empty observation, not proof of receiver failure.'),
'E12': ('MIB library', '/settings/general?tab=mibs', 'Zero installed MIBs. UI explicitly states uploaded files are stored on disk and runtime compilation will arrive in a later update.'),
'E13': ('Remote sensor fleet', '/settings/general?tab=sensors', 'One VMware sensor, v1.23.5, seven assignments, heartbeat approximately two days old, offline. OVA/OVF/QCOW2/VHDX-related deployment artifacts available. Offline sensor cannot demonstrate remote polling continuity.'),
'E14': ('Reports', '/reports', 'Library includes availability, performance, inventory, alerts and headroom reports. Custom builder assembles fixed sections. No generated report history and no schedules in /reports/schedules. PDF/HTML and some Excel/CSV export controls visible; exports and delivery were not executed.'),
'E15': ('Availability', '/availability', 'One-hour fleet view explicitly said measured over 14 minutes. 54.65% availability was a transient mixed-estate sample, not a full-hour production SLA result. Coverage disclosure exists but operational acceptance remains unproven.'),
'E16': ('Access controls', '/settings/general?tab=users', 'Administrator and viewer roles visible; viewer visibility limited to a tag. Roles/permissions and authentication tabs present. No separate-account isolation or login test was performed.'),
'E17': ('Capacity view', '/netflow/capacity', '95th-percentile utilization compared with interface speed. No traffic in selected window. This is a headroom/billing view, not an exhaustion-date forecast.'),
}

CODE = {
'C01': ('SNMP collector and counter handling', 'poller/internal/checker/snmp/collector.go', 'High-capacity interface OIDs at lines 826-829; rate wrap/reset logic around 1094. Vendor collection and standard polling exist. No loaded-link accuracy test executed.'),
'C02': ('Standard network tables', 'poller/internal/checker/snmp/standard_network_groups.go', 'BGP at line 23, OSPF 33, VRRP/VRRPv3 42/53, STP 65/75, LACP 85. These are collector capabilities, not proof of a complete routing dashboard.'),
'C03': ('Network alert evaluator', 'server/app/services/network_alert_service.py', 'Supported canonical metric set at 43-55; template evaluation 204-246; selection/evaluation loop 468-566; apply 569-613. Compound fields are fetched but not used in the reviewed evaluation branches. Template conditions use latest values, not continuous-duration proof.'),
'C04': ('Rule API contract', 'server/app/api/v1/alert_rules.py', 'Canonical fan/PSU/VPN/HA/BGP keys accepted at 28-30; conditions mirror the first condition to legacy fields at 386-394. Compare against C03 before claiming evaluator parity.'),
'C05': ('Dependency suppression', 'server/app/api/v1/alert_engine.py', 'Upstream lookup at 381; suppression in event-driven paths around 707-843 and 1181-1278. This is not evidence of equivalent suppression in periodic SNMP evaluation.'),
'C06': ('NetPath collector / API', 'poller/internal/checker/netpath/prober.go', 'IPv4-only rejection at 109. Related API server/app/api/v1/netpath.py exposes snapshots, graph, hops, routes, events and comparison. Remote probe parity not demonstrated.'),
'C07': ('Discovery schema', 'server/app/schemas/discovery_v2.py', 'Scope/import/schedule types 12-16; protocols and credential references 33-38; classifications and limits 39-56. Local implementation corroborates empty live workflows.'),
'C08': ('Trap protocol boundary', 'poller/internal/checker/snmp/traps.go', 'Lines 39-40 explicitly defer v3 traps; current listener is v1/v2c. SNMPv3 polling and SNMPv3 trap reception are separate capabilities.'),
'C09': ('Map assistance', 'server/app/api/v1/manual_maps.py', 'LLDP/CDP link assistance begins at 603; uses topology_links around 654. Existing canvas links and auto-discovered topology must not be treated as identical lifecycle behavior.'),
'C10': ('Reporting capacity sections', 'server/app/services/report_sections.py', 'Capacity sections around 1209-1250 calculate present filesystem/link headroom. Template at 1531-1535 selects these sections. No forecast implementation found in scoped searches.'),
'C11': ('Vendor coverage migrations', 'scripts/migrate-080-snmp-network-device-coverage.sql', 'Adds CX/OS10 and extends Cisco, FortiGate, PAN-OS and MikroTik. Earlier 062/063 migrations define the main packs. Presence in source does not certify every device/firmware combination.'),
'C12': ('Authentication and role foundation', 'server/app/services/external_auth.py', 'LDAP/RADIUS and group-to-role mapping implemented; server/app/models/role.py stores permission lists. SAML/OIDC and enforcement coverage were not established.'),
'C13': ('Capture foundation', 'server/app/services/network_capture_service.py', 'Bounded on-demand capture lifecycle; agent implementation under ZenPlus_Agent/internal/netcapture. This does not establish continuous SPAN-based application/network response-time analytics.'),
}

ROWS=[]
def row(category, feature, baseline, status, evidence, gap, priority, epic, source, scope='NPM'):
    ROWS.append(dict(id=f'F{len(ROWS)+1:02}',category=category,feature=feature,baseline=baseline,status=status,evidence=evidence,gap=gap,priority=priority,epic=epic,source=source,scope=scope))

cat='Discovery and core polling'
row(cat,'Network discovery and classification','Network discovery','Available / unproven','E04 C07','Wizard and backend exist; no discovery runs demonstrated. Validate multiple subnets, exclusions, duplicates and identity changes.','P0','A','S02')
row(cat,'Recurring discovery and import policy','Scheduled discovery','Available / unproven','E04 C07','Recurring/cron and review/auto-match modes in source; no configured profiles or import history. Enable a controlled pilot.','P1','B','S02')
row(cat,'Resource selection and bulk management','Manage nodes and resources','Partial','E01 E03 E04','Device/interface selection, filtering and exports present; verify rediscovery preserves interface identity and monitoring choices.','P1','B','S02')
row(cat,'ICMP status, RTT and packet loss','Availability and response','Observed','E01 E02 E15','Current checks and history visible. Establish production scope and missing-check policy before relying on fleet SLA.','P0','A','S05')
row(cat,'SNMP v1/v2c/v3 polling','SNMP polling','Partial','E02 E07 C01','v2c demonstrated; v3 model/session support in source. AuthPriv, context and collector-side behavior need live certification.','P1','B','S05')
row(cat,'IPv6 network monitoring','IPv6-capable monitoring','Unverified','C02 C06','Do not infer IPv6 parity from IPv6 session counters. Exercise IPv6 discovery, ICMP, SNMP and tables; NetPath collector is IPv4-only.','P1','B','S02')
row(cat,'CPU, memory, uptime and identity','Device performance','Observed','E02 E07 E08','Vendor and standard metrics visible on lab examples. Validate numerical accuracy on representative real models.','P0','A','S01')
row(cat,'Polling intervals and diagnostics','Configurable polling','Partial','E02 C01 C07','Ping interval, SNMP diagnostics and bounded table cadence exist. Fleet poll completion, timeout causes and stale-resource health need validation.','P1','B','S05')
row(cat,'Historical data and coverage','Real-time and historical views','Partial','E02 E15','Charts disclose gaps, but 15-22% coverage was seen on one-hour lab windows. Confirm retention, completeness and consistent unknown handling.','P0','A','S01')
row(cat,'Tags, groups and custom properties','Group and property scoping','Partial','E01 E16','Tags/location/groups and tag-limited visibility exist. Arbitrary typed properties and dynamic group rules were not demonstrated.','P2','B','S02')

cat='Interfaces and device hardware'
row(cat,'Interface inventory and status','Interface monitoring','Observed','E03','47 links and per-device tables visible, including speed and status. Retain this foundation; validate reindex and administrative-down semantics.','P1','B','S02')
row(cat,'Bandwidth and utilization','Receive/transmit utilization','Partial','E03 C01','64-bit counter collection exists; live throughput was zero/dashes. Prove rates and capacity denominators against known traffic.','P0','A','S01')
row(cat,'Errors and discards','Interface health statistics','Partial','E03 C01','Fleet filters and reset-safe counter views exist; nonzero fault examples and rate/interval accuracy were not demonstrated.','P1','B','S02')
row(cat,'Link flaps and downtime','Interface outage diagnostics','Partial','E03','Flapping-link counters and detail views present. Test up/down sequences, missing samples and per-link timelines.','P1','B','S02')
row(cat,'Custom interface capacity','Per-interface configuration','Observed','E03','Manual speed/bandwidth control visible. Prove asymmetric circuit calculations and direction labels.','P1','B','S02')
row(cat,'Duplex mismatch diagnostics','Detect/predict mismatch','Not found','E03 C01','No dedicated duplex state or mismatch diagnosis located. Add polling, peer correlation and actionable alerts for supported media.','P2','D','S02')
row(cat,'Physical device / port layout','Device View stencils','Not found','E02 E03','Interface tables are present; chassis/slot/port stencils with live status were not found.','P2','D','S02')
row(cat,'Temperature, fans and PSU','Hardware health','Partial','E02 E07 E08','Useful sensor panels exist, but headline health does not reflect several displayed failures. Add component-aware health and certify alert paths.','P0','A','S27')
row(cat,'Switch stacks and redundancy','Stack monitoring','Partial','E02 E07 C11','Cisco members/ring observed; CX VSF pack exists. Juniper Virtual Chassis and complete cross-vendor stack health not demonstrated.','P1','D','S02')
row(cat,'PoE, optics and modular hardware','Device-specific hardware','Partial','E02 E07 C11','PoE visible and MikroTik optics defined. Broader optics, line-card state, reset reasons and inventory relationships need coverage.','P2','D','S04')

cat='Topology and network protocols'
row(cat,'Manual maps and NOC display','Maps and NOC views','Observed','E06','Rich editable map with live mode and interface labels exists. The inspected map was QA/simulator data.','P1','B','S01')
row(cat,'LLDP/CDP topology collection','Topology relationships','Partial','E06 C09','Quick-connect consumes discovered adjacency. Validate refresh, stale links, port resolution and multi-vendor discovery.','P1','D','S01')
row(cat,'Automatic topology lifecycle','Dynamic topology mapping','Partial','E06 C09','Manual canvas assistance is not proof of automatically maintained topology. Add change history, aging and confidence for discovered links.','P1','D','S01')
row(cat,'Dependencies and impact isolation','Dependency-aware alerting','Partial','E09 C05 C03','Event-driven suppression exists; periodic SNMP parity and an operator-facing dependency workflow were not established.','P0','C','S01')
row(cat,'BGP and OSPF neighbor health','Routing neighbor visibility','Partial','E07 C02','BGP/OSPF tables exist in collector; no complete routing workflow demonstrated. Canonical BGP alert key needs evaluator mapping.','P1','D','S02')
row(cat,'Routing tables and route changes','Route visibility','Not found','C02 E06','Dedicated route inventory, next-hop correlation and route/default-route change views not located. Scope original NPM versus newer Insights separately.','P1','D','S02 S17')
row(cat,'VRRP, STP and LACP health','Network protocol context','Partial','C02','Standard tables implemented. Dedicated protocol relationships, transitions and object-level alert lifecycle still need verification.','P1','D','S02')
row(cat,'Multicast groups and routing','L3 multicast monitoring','Not found','E06 C02','No multicast routing/group collector or tree workflow found. Flow protocol classification is not multicast routing monitoring.','P2','D','S16')
row(cat,'Fleet routing insights, VRFs and peer correlation','Unified Routing Insights','Not found','C02 E06','Candidate extension: no unified VRF/peer/route interface was found. Official fleet Routing Insights requires Observability Self-Hosted licensing.','P2','D','S17 S18','Extension')

cat='Vendor, wireless and hybrid coverage'
row(cat,'Cisco ASA insight','ASA Network Insight','Partial','E07','19-metric pack includes connections, failover and remote-access counts; zero devices attached. Certify tunnels, users and object drill-down.','P1','E','S27')
row(cat,'Cisco Nexus vPC insight','Nexus Network Insight','Not found','E07 C11','No dedicated NX-OS/vPC collector or workflow located. IOS/XE metrics do not establish Nexus peer/keepalive/member-port parity.','P1','E','S11')
row(cat,'Cisco ACI logical / physical context','ACI monitoring','Not found','E07','No APIC integration or tenant/application-profile/EPG/fabric workflow found. Build only against a representative fabric and fixed scope.','P2','F','S27')
row(cat,'F5 LTM component monitoring','F5 virtual servers and pools','Partial','E07','39-metric SNMP pack provides virtual-server/pool health, traffic, HA and hardware. Relationship graph and pool-member-level parity not demonstrated.','P1','E','S12 S13')
row(cat,'F5 DNS/GTM and health monitors','GTM and iControl','Not found','E07','No GTM/Wide-IP hierarchy or iControl health-monitor workflow located; optional pool-member rotation controls also absent.','P2','E','S12')
row(cat,'Palo Alto firewall insight','Palo Alto Network Insight','Partial','E07','PAN-OS pack defines sessions, CPU, HA and GlobalProtect. Complete site-to-site tunnel state/traffic/encryption detail is not established.','P1','E','S04 S02')
row(cat,'FortiGate monitoring','Fortinet network monitoring','Partial','E08','Rich SNMP panels demonstrated on a simulator. Validate HA/VDOM/VPN/SD-WAN and managed-child behavior on physical or virtual FortiOS devices.','P1','E','S04 S08')
row(cat,'Juniper, Aruba, Dell, MikroTik and other vendors','Multi-vendor device coverage','Partial','E07 C11','Good starting packs. Some have no attached devices; Arista/Extreme and broader model/firmware certification remain gaps against current catalog.','P2','E','S02 S04')
row(cat,'Wireless controller and AP inventory','Wireless monitoring','Partial','E07 E08','Aruba/FortiGate AP and client totals exist. No unified wireless inventory or Cisco WLC monitoring pack demonstrated.','P1','E','S08')
row(cat,'Per-client wireless performance','Client, SSID and radio detail','Not found','E07 E08','No client-level RSSI/SSID/radio/channel history located. Aggregate client counts and UDT endpoint records do not meet this capability.','P1','E','S10')
row(cat,'Rogue APs and RF coverage maps','Wireless diagnostics','Not found','E06 E07','No rogue-AP workflow or RF heat map found. SolarWinds heat maps have Cisco-controller restrictions and legacy Network Atlas dependencies.','P2','E','S08 S09')
row(cat,'Cloud-managed wireless APIs','Cloud controller monitoring','Not found','E07','Meraki, Aruba Central, Mist, Ruckus and similar API connectors not found. Add vendor API pagination, rate limits and health diagnosis.','P1','F','S08 S10')
row(cat,'SD-WAN orchestrators and overlay','Orchestrator/edge/uplink monitoring','Partial','E08','FortiGate SNMP WAN health exists. No VeloCloud/Catalyst/Prisma/FortiManager orchestration integration or overlay inventory found.','P1','F','S14 S04')
row(cat,'Azure gateways and VPN connections','Azure network monitoring','Not found','E07','No Azure gateway/site-to-site account integration found. NPM scope includes gateways/connections, not all cloud observability.','P2','F','S15')

cat='Troubleshooting and customization'
row(cat,'Continuous hop-by-hop path monitoring','NetPath','Observed','E05','Live TCP SYN probe, multiple flows, latency/loss and branching path present. Retain and validate path semantics and target reachability.','P1','B','S19')
row(cat,'Path history and route comparison','Historical NetPath','Observed','E05 C06','History, per-hop, routes, events and compare controls plus backend endpoints present. Stale/demo result status needs explicit handling.','P0','A','S19')
row(cat,'Multiple network-path vantage points','Distributed path probes','Unverified','E05 E13 C06','Live path source was Appliance. An offline remote sensor is not proof of branch NetPath execution, replay or reliable source identity.','P1','F','S19 S26')
row(cat,'Shared cross-metric timeline','PerfStack','Not found','E02 E05 E14','Individual charts exist; no saved shared-timeline investigation across devices, interfaces, paths, alerts and applications located.','P1','G','S19')
row(cat,'Custom OID scalar/table polling','Universal Device Pollers','Partial','E07 C01','Template groups, counters, enums and scale are implemented. Validate arbitrary custom OIDs, row identity and reusable import/export workflows.','P1','B','S06')
row(cat,'MIB resolution and derived formulas','MIB and poller customization','Partial','E12 C01','MIB upload is storage-only. No complete symbolic MIB browser/compile path or arbitrary multi-poller formula editor demonstrated.','P1','B','S06 S07')
row(cat,'Network capacity forecasts','Exhaustion-date prediction','Not found','E17 C10','Headroom and 95th percentile exist, but no CPU/memory/interface exhaustion forecast found. Add trend models and data-quality gates.','P1','G','S20')
row(cat,'Dynamic statistical thresholds','Baseline thresholds','Not found','E09 C03','Static thresholds are present. Server software baselines are a different feature; network baseline learning was not found.','P1','G','S21')
row(cat,'Passive application/network timing','Quality of Experience','Partial','C13','On-demand connection capture exists in source. No continuous SPAN/server sensor pipeline showing network versus application response time established.','P2','F','S24 S25')

cat='Alerting, logs and reporting'
row(cat,'Threshold rules and recovery','Advanced alerting','Partial','E09 C03','Core scalar/interface rules exist; verify all supported metrics, clear conditions and mixed-engine behavior before operational sign-off.','P0','C','S22')
row(cat,'AND/OR compound conditions','Multi-condition alerts','Partial','E09 C03 C04','Wizard exposes AND/OR. Reviewed periodic network evaluator selects legacy metric fields and does not apply the conditions array. Deployment verification required.','P0','C','S01')
row(cat,'Hold time and reset hysteresis','Sustained thresholds','Partial','E09 C03','Scalar sustained logic exists; template rules evaluate latest values from a lookback. Complete reset/hold semantics across every evaluator.','P0','C','S22')
row(cat,'Component-specific alert lifecycle','Hardware/protocol alerts','Partial','E07 E09 C03 C04','Canonical fan/PSU/VPN/HA/BGP keys are accepted but absent from periodic supported set. tpl_* provides an alternative, with device-level aggregation.','P0','C','S13 S27')
row(cat,'Notifications and escalation','Alert actions and escalation','Available / unproven','E09 E10','Channels, cooldowns and escalation UI exist; zero channels configured and both gateways disabled. No real delivery was tested.','P0','A','S27')
row(cat,'Maintenance, snooze and dependencies','Alert suppression','Partial','E02 E09 C03 C05','Maintenance/snooze controls exist. Prove node-down, dependency, schedule and maintenance suppression consistently across metric and event engines.','P0','C','S04')
row(cat,'ITSM and collaboration workflows','Platform integrations','Partial','E10','Webhook/Slack/Telegram cataloged ready; Teams and Jira/ServiceNow explicitly planned. Generic webhook does not establish bidirectional incident sync.','P2','G','S27','Platform')
row(cat,'SNMP trap reception and history','Trap ingestion','Available / unproven','E11 C08','v1/v2c receiver and feed present; no selected-window records. v3 traps explicitly deferred in source. Certify actual trap-to-alert delivery.','P1','C','S23')
row(cat,'Network-device syslog workflow','Log Viewer','Not found','E11 C08','No network syslog receiver/search/rule workflow found. Server logs and a Syslog port label do not establish ingestion.','P1','C','S23')
row(cat,'Alert/event history and acknowledgement','Operational alert console','Observed','E01 E02 E09','Live events, active alerts and acknowledge controls visible. Alert storm, grouping, stale-state and operator workflow tests remain.','P1','C','S27')
row(cat,'Availability/SLA and outage reports','Availability reporting','Partial','E14 E15','Dashboards and report templates exist. Separate sampled-check availability from contractual uptime and expose coverage, maintenance and denominators.','P0','A','S02')
row(cat,'Custom reports and scheduled export','Report builder and schedules','Partial','E14','Fixed-section builder and export controls exist; arbitrary object/field/query reporting absent from inspected builder. No schedules or generated-report history.','P1','G','S02')

cat='Platform and enterprise readiness'
row(cat,'Role and object access controls','Roles and scoped access','Partial','E16 C12','Viewer tag scope and permission model visible. Read-only inspection cannot prove enforcement on API, reports, exports and nested objects.','P1','H','S27','Platform')
row(cat,'Directory authentication and SSO','Enterprise authentication','Partial','E16 C12','LDAP/RADIUS foundation exists; no authenticated integration test performed. SAML/OIDC equivalence not established.','P2','H','S27','Platform')
row(cat,'API and auditability','Platform extensibility','Partial','C04 C06 C12','FastAPI and audit logging exist in source. Public integration contract, scoped API lifecycle and complete audit coverage need certification.','P2','H','S27','Platform')
row(cat,'Distributed polling and buffering','Remote collectors','Partial','E13','Sensor deployment and assignments exist, but the only sensor is offline. Verify outage buffering, replay, timestamping and capability compatibility.','P0','A','S26','Platform')
row(cat,'Polling capacity and load distribution','Additional polling engines','Unverified','E01 E13','40-device mixed estate is not a scale benchmark. No measured sustainable element capacity, engine balancing or saturation behavior established.','P1','H','S26','Add-on')
row(cat,'Monitoring-system high availability','HA deployment','Not found','E13','No controller/collector failover pools or proven database failover found. Device HA panels monitor equipment; they do not protect ZenPlus itself.','P1','H','S04 S27','Add-on')
row(cat,'Retention, backup and restore','Operational continuity','Unverified','E14 E15','Settings exist in navigation; historical continuity, restore integrity and disaster recovery were not exercised. Define and prove RPO/RTO.','P1','H','S26','Platform')

STATUS_DEFS = {
'Observed':'The narrow feature was visible with current records or concrete controls. This is not an end-to-end parity certification.',
'Partial':'An implementation exists, but a specific depth, semantic, coverage or operational gap remains.',
'Available / unproven':'The workflow exists but is unconfigured, empty or not exercised on representative data.',
'Not found':'No implementation was located in the inspected UI and scoped source review, or the UI explicitly labels it deferred/planned. This is an evidence-bounded finding.',
'Unverified':'The inspection cannot determine working support or its limits. A dedicated acceptance test is required.',
}

EXEC_SUMMARY = [
'Assessment: ZenPlus has a substantial network-monitoring foundation, but full SolarWinds NPM replacement is not demonstrated by this appliance. Continue it as a controlled pilot until telemetry, alert semantics, vendor depth and operational resilience meet explicit acceptance gates.',
'The strongest foundations are ICMP/SNMP monitoring, an extensible vendor-template system, interface inventory, hardware panels, live NetPath, manual network maps, alert configuration and report templates. Existing capability should be extended rather than rebuilt.',
'The highest-risk gaps are operational correctness: no notification destinations, an offline remote sensor, short data coverage, healthy headline scores alongside failed components, and source-level mismatches between alert configuration and periodic SNMP evaluation.',
'The major product-depth gaps are automatic topology lifecycle and routing views, Nexus/ACI and cloud-controller integrations, full wireless client/RF visibility, F5 relationship depth, exhaustion forecasting, dynamic baselines, a PerfStack-like shared timeline, compiled MIB tooling, syslog and demonstrated QoE/HA/scale.',
'Recommendation: spend the first two weeks proving the monitoring and alerting foundation. Target a qualified core-network release in 8-12 weeks and a broader enterprise release in roughly 20-28 weeks with the staffing assumptions below. Full vendor-catalog parity is a continuing certification program, not a short UI project.',
]

METHODOLOGY = [
'Live scope: existing administrator session on the requested appliance, observed on 10 September 2026 at approximately 15:27-15:37 Asia/Riyadh. Read dashboards, tables, detail views and unsaved editors. No device settings, rules, credentials, deployments, discovery scans, packet captures or outgoing test messages were changed or triggered.',
'Source corroboration: local C:/Users/user/Documents/ZenPlus checkout, .version 1.23.10 and HEAD b4256ff. The working tree contained unrelated modifications. Same version labels do not prove deployed binary/source identity; source findings are explicitly provisional for the appliance until deployment hash and runtime behavior are verified.',
'Comparator: full NPM functional scope and its shared SolarWinds Platform features, checked against current official documentation and NPM 2026.2 release notes. The directly opened documentation index advertised 2026.2.2; a search cache advertised 2026.2.1. This report uses feature documentation, not a claim that all patch-specific changes were validated. [S03 S04]',
'License boundaries: NTA, NCM, UDT, IPAM, VNQM and EOC are not silently counted as NPM. HA and additional polling engines have deployment/licensing dependencies. Newer fleet Routing Insights explicitly requires Observability Self-Hosted; it is labeled Extension. Validate current entitlements before procurement or a contractual parity claim. [S17 S26 S28]',
'Coverage: the matrix is an operational capability checklist across the documented NPM feature families, not a certification of every OID, supported device SKU, firmware release, integration action or deployment size. SolarWinds itself was assessed from official documentation, not a parallel live installation.',
'Evidence limits: most deep vendor examples were lab/simulator records on loopback addresses. No induced network failure, sustained-load test, discovery/import run, outbound notification, alternate-role test or failover test was performed. Empty data is classified separately from missing implementation.',
'No single parity percentage is reported. Equal weighting would hide alert reliability and data correctness behind many small UI features. Status counts describe only the rows in this report; readiness is governed by the acceptance gates.',
]

FINDINGS = [
('P0-01 | Notification delivery is not operational','Observed: /channels has zero destinations; default SMTP and SMS gateways are disabled. Alert configuration exists, but operators have no configured delivery path in this channel system. Create approved destinations and demonstrate trigger, acknowledgement/escalation and recovery with a controlled test. Evidence E09-E10.'),
('P0-02 | Remote monitoring is not demonstrated','Observed: VMware-Probe-01 is offline with a heartbeat about two days old and seven assignments. Version differs from controller. Restore sensor health and verify assignment compatibility, result freshness, offline buffering and replay. Do not equate a downloadable sensor image with a working branch deployment. Evidence E13.'),
('P0-03 | Health score can obscure component failures','Observed: FortiGate lab score is 100/100 while HA sync, VPN, WAN and hardware panels show faults; Cisco score is 100/100 with a fan warning. The page explains a CPU/memory/loss score, so this is principally a misleading overall-health presentation. Separate reachability, performance, component health and data freshness, then define an explicit roll-up policy. Evidence E02/E08.'),
('P0-04 | Historical completeness needs a production acceptance gate','Observed: selected one-hour periods covered only about 15-22% on example nodes and 14 minutes on fleet availability. The UI discloses coverage, which is useful. The underlying cause was not diagnosed; retention, poll gaps, restart or test-data behavior are hypotheses. Prove continuity and never treat sparse 100% observed-check uptime as a full-window SLA. Evidence E02/E15.'),
('P0-05 | Compound SNMP alert semantics need correction/verification','Source finding: C04 mirrors conditions[0] into legacy fields. C03 fetches conditions/condition_logic but its reviewed scalar/interface/template branches evaluate the legacy rule metric rather than the compound expression. A rule such as CPU >90 AND memory >90 could be evaluated using only its first condition in this path. Verify deployed code identity; add engine-level truth-table tests before certification.'),
('P0-06 | Some selectable state metrics lack a canonical evaluator path','Source finding: fan_state, psu_state, vpn_tunnel_state, ha_state and bgp_neighbor_down are accepted by the rule API but absent from C03 NETWORK_METRICS. Template metrics are supported separately, so the gap is not absence of all hardware/VPN alerting. Map canonical keys to collected entities or reject unsupported definitions. Prove each key triggers and clears on the appliance. Evidence C03/C04.'),
('P0-07 | Hold time and suppression differ between engines','Source finding: template conditions use latest values in a lookback window; that does not prove continuous breach for the configured duration. Periodic SNMP paths do not show the upstream dependency checks used by event-driven alerts. Build a single semantic contract for holds, reset conditions, dependencies, maintenance and no-data. Evidence C03/C05.'),
('P1-01 | Several capability labels overstate demonstrated depth','Observed: MIB upload explicitly defers runtime compilation; Capacity Planning is headroom/95th percentile, not forecasting; custom reports assemble fixed sections; Teams/ITSM are planned. Retain useful existing features, but label and prioritize the missing portions clearly. Evidence E10/E12/E14/E17.'),
('P1-02 | Stale status and lab inventory affect operator trust','Observed: a demo NetPath probe showed OK with a 28-day-old check. Fleet records include loopback and QA devices. Add explicit demo/test classification, freshness expiry and a production-only reporting scope. Do not delete or reclassify existing records without agreed ownership. Evidence E01/E05/E06.'),
]

BOUNDARIES = [
('NetFlow traffic analytics','NTA','ZenPlus has NetFlow overview, forensics, anomalies and capacity screens; the inspected windows contained no flows. Treat as an adjacent capability, not proof of NPM depth or NTA parity.'),
('Configuration backup/compliance/firmware','NCM','Config Backup is present in ZenPlus navigation. A backup menu does not establish configuration compliance, approval, vulnerability, rollback or firmware-upgrade parity.'),
('Endpoint/MAC/user/switch-port tracking','UDT','ZenPlus has an extensive UDT navigation surface and related collector code. A full UDT assessment was outside this NPM review.'),
('IP address and DHCP/DNS management','IPAM','Do not mark IPAM absence as an NPM deficiency. Assess separately if network-management-suite replacement is intended.'),
('VoIP quality and IP SLA operations','VNQM','Do not count full voice/call-manager/IP-SLA coverage as an NPM requirement without expanding scope.'),
('Deep server/application/virtualization observability','SAM / VMAN / other','ZenPlus APM and server modules extend beyond this review. They do not compensate for missing network-device functions.'),
('Multiple-instance enterprise console','EOC','Separate federation requirement; a single controller plus remote sensors is not evidence of a multi-instance enterprise console.'),
]

EPICS = [
dict(id='A',name='Establish trustworthy live monitoring',when='Weeks 1-2',effort='4-6 person-weeks',owner='SRE + backend + QA',depends='None',deliver='Create an owned pilot inventory; separate lab/demo reporting; restore remote sensor; configure approved notification destinations; diagnose interface/coverage gaps; add freshness and component health summaries.',accept='At least 20 representative real network nodes across two sites, >=100 interfaces and 14 days of evidence. >=99% expected poll completion at the chosen intervals; overdue data becomes unknown within two intervals; known component faults are visible in summary. Every approved delivery channel passes trigger/escalation/recovery. Record exclusions.',features='F01 F04 F07 F09 F12 F18 F45 F55 F61 F66'),
dict(id='B',name='Harden discovery and polling contracts',when='Weeks 2-6',effort='6-9 person-weeks',owner='Collector + backend + QA',depends='A: representative inventory',deliver='Certify SNMPv3 and IPv6 core polling; resource selection/reindex behavior; counter wrap/reboot/discontinuity semantics; poller health; custom OID authoring; MIB resolve/compile workflow and reusable poller export/import.',accept='Discover a controlled subnet twice without duplicate identities. Preserve intended monitoring after ifIndex changes. Certify v2c/v3, 1/10/100-Gbps counters as applicable, resets and missing samples against device/traffic-generator reference. Custom scalar/table and transformed metric are usable in charts and alerts.',features='F01-F17 F21 F44 F48-F49'),
dict(id='C',name='Unify alert semantics and network events',when='Weeks 1-6',effort='8-12 person-weeks',owner='Backend lead + collector + QA',depends='A for live tests; can start code review immediately',deliver='One evaluator contract for AND/OR, sustained conditions, reset hysteresis, schedules, dependencies, maintenance, per-component identity and no-data. Add canonical metric capability validation, syslog ingestion and certified trap reception.',accept='Truth-table tests cover every supported metric family. One child-down event under failed parent produces one primary notification and records suppressed children. Independent VPN tunnels raise/clear independently. No early trigger during hold; no duplicate page on replay; real syslog/trap-to-alert-to-channel test succeeds.',features='F24 F25 F52-F60'),
dict(id='D',name='Complete topology and protocol workflows',when='Weeks 5-10',effort='8-12 person-weeks',owner='Collector + UI + QA',depends='B identity/counters; C object alerting',deliver='Discovered topology with aging and change history; explicit dependencies; BGP/OSPF route/neighbor detail; STP/LACP/VRRP relationships; duplex diagnostics and stack depth. Stage multicast and physical device views by demand.',accept='Compare a two-site reference topology with LLDP/CDP and device CLI. Link removals age correctly; routed next hops and neighbors map correctly. Simulated peer/stack/link failures show impact and suppress child noise. Multicast/VRF extensions have separate acceptance scope.',features='F06 F16-F29'),
dict(id='E',name='Certify vendor insight and wireless depth',when='Weeks 7-16',effort='14-22 person-weeks',owner='Two collector/integration engineers + UI + QA',depends='B custom metrics; C component alerts; D relationships',deliver='Prioritize installed device families. Add Nexus vPC, complete ASA/PAN-OS tunnels, F5 pool members/relationships and optional GTM/iControl; implement Cisco WLC plus wireless clients/SSID/radio history. Expand hardware/stack coverage with a model/firmware matrix.',accept='For each supported family use at least two representative firmware/model combinations where feasible, plus a documented negative/unsupported case. Compare counters and states with vendor output. Prove component failure/recovery and no stale health. Wireless client identity and RF metrics match controller data.',features='F18-F20 F30-F40'),
dict(id='F',name='Add hybrid and distributed diagnostics',when='Weeks 11-22',effort='12-20 person-weeks',owner='Integration + collector + UI + QA',depends='B/C/D; actual controller/API access',deliver='Select one SD-WAN and one cloud-wireless provider first; then expand. Add Azure gateway integration and ACI if deployed. Certify remote NetPath. Design continuous packet-based QoE as a separately sized workstream.',accept='Controller entities reconcile without duplicates under pagination, credential expiry and API throttling. Tunnel uplink metrics match vendor console. Same target measured from two sites retains correct source identity. QoE, if selected, separates network and application timing under controlled induced latency.',features='F32 F41-F43 F46 F51'),
dict(id='G',name='Forecasting, investigations and reporting',when='Weeks 7-14',effort='8-12 person-weeks',owner='Backend/data + UI + QA',depends='A history; B metric metadata; C events',deliver='Shared time-aligned investigation workspace; saved/shareable read-only views; baseline thresholds; interface/CPU/memory forecasts; richer object-field reports; scheduled delivery. ITSM integration is optional scope.',accept='Overlay interface, device, path and alert data at one timestamp. Forecasts use peak/average trends, require sufficient history and suppress misleading dates on flat/noisy/sparse data. Backtest on held-out periods. Report and exported data match source queries and preserve access limits.',features='F47 F50 F51 F57 F62'),
dict(id='H',name='Enterprise reliability and release gates',when='Weeks 9-24; certification buffer to week 28',effort='10-16 person-weeks',owner='SRE + platform/backend + QA/security',depends='A-C stable; target scale agreed',deliver='Define supported element capacity; load and soak testing; HA/DR architecture and deployment automation; collector reassignment; backup/restore; RBAC/API/export isolation; authenticated integration documentation.',accept='Run 72-hour load tests at agreed target scale and a 30-day pilot. Proposed starting target: 1,000 nodes / 10,000 interfaces, validated at chosen polling intervals rather than claimed in advance. Demonstrate controller failover with no duplicate actions, RTO <=5 min and RPO <=1 min if architecture supports them; otherwise publish measured limits.',features='F63-F69'),
]

PLAN_NOTES = [
'Estimates are planning judgments, not measured implementation commitments. Total work is approximately 80-129 person-weeks including the listed QA effort. Allow 20-28 calendar weeks for broad scope with five effective delivery engineers (backend/platform, two collector/integration, frontend and QA/SRE), plus part-time product/network ownership. Dependencies, firmware access and a 30-day soak prevent simply dividing effort by headcount.',
'An 8-12-week release should be called qualified core network monitoring, not full NPM parity. Its mandatory gates are A-C plus the topology and device families actually needed by the pilot. Keep other families explicitly unsupported or experimental until certified.',
'The full plan requires access to representative routers, switches, firewalls, controllers, load balancers, branch networks and cloud APIs. Confirm the device/firmware inventory and target scale in week 1. If those products are not deployed, defer ACI, GTM, niche wireless vendors, RF heat maps and multicast without weakening the immediate operational gates.',
'Use feature flags and versioned metric schemas. Pilot collectors and vendor packs on selected devices, maintain schema compatibility with older sensors, stage rollout by site, and have reversible application deployment plus a tested database recovery procedure. Do not use production network outages to validate alerts.',
'Acceptance targets above are proposed engineering requirements. They are not observed ZenPlus performance figures or claimed SolarWinds service guarantees. Tune budgets, polling intervals, SLOs and HA targets once actual fleet and hardware sizing are known.',
]

TESTS = [
('T01','Polling correctness','Compare expected polls with persisted timestamps; v2c/v3, timeouts and IPv6; prove no-data differs from zero or down.','A/B'),
('T02','Traffic accuracy','Known traffic at several rates; counter wrap, restart, link-speed change and ifIndex change; direction-specific utilization.','B'),
('T03','Discovery lifecycle','Repeated scan, new interface, removed device, changed IP/hostname, duplicates, exclusions and import audit.','B'),
('T04','Alert semantic contract','AND/OR truth tables, hold, reset, recovery, maintenance, schedules and all canonical/template state metrics.','C'),
('T05','Fault impact and notification','Parent failure suppresses children; independently failing tunnels; message retries and replay; acknowledgement stops escalation.','A/C/D'),
('T06','Vendor correctness','Model/firmware matrix with real reference output; unsupported OIDs explicit; sensor alarms propagate to summary.','D/E'),
('T07','Wireless and controller lifecycle','Roaming/disconnect, client/SSID/radio association, API pagination/rate limits/credential expiry, stale children.','E/F'),
('T08','Path correctness','Multi-flow branch behavior, target timeout versus intermediate ICMP silence, history comparison, stale expiry and source identity.','B/F'),
('T09','Analytics and reports','Coverage-aware SLA, forecast backtesting, synchronized timeline, exported values and scheduled-channel delivery.','G'),
('T10','Enterprise recovery and access','72-hour scale test, 30-day soak, collector outage replay, controller/database failure, restore and cross-role API/export isolation.','H'),
]

DECISIONS = [
'Which device vendors, models and firmware versions must be supported at launch? Prioritize the installed estate over an undifferentiated vendor count.',
'What are the target nodes, interfaces, wireless clients, traps/syslogs per second, polling intervals and retention? Device count alone is insufficient for sizing.',
'Is the commercial target standalone NPM replacement or the broader NAM/Observability suite? Keep adjacent modules and licensed extensions in separate acceptance scopes.',
'What are the notification destinations, service ownership, maintenance policy, SLA formula and disaster-recovery objectives? These determine operational readiness.',
]

COUNTS = Counter(r['status'] for r in ROWS)
for epic in EPICS:
    epic['features'] = ', '.join(r['id'] for r in ROWS if r['epic'] == epic['id'])
METHODOLOGY[0] = METHODOLOGY[0].replace('15:27-15:37', '15:27-15:41')
EVIDENCE['E16'] = (EVIDENCE['E16'][0], EVIDENCE['E16'][1], EVIDENCE['E16'][2] + ' Authentication tab explicitly showed LDAP/AD and RADIUS not configured; only local sign-in was active.')
ROWS[5]['gap'] = 'IPv6 discovery, ICMP, SNMP and tables require certification. Local NetPath is IPv4-only; this report does not assume SolarWinds NetPath supports IPv6.'

# Revision 2: user confirmed this is a development appliance without real SNMP devices.
SCOPE = 'ZenPlus v1.23.10 | Development appliance | Revision 2: environment corrected'
EXEC_SUMMARY = [
'Assessment: ZenPlus has a substantial network-monitoring implementation with identifiable feature-depth gaps against NPM. This is a development capability review, not a production-readiness verdict. Real-device correctness and scale remain untested.',
'Existing foundations include ICMP/SNMP collection, vendor templates, interface and hardware panels, NetPath, manual maps, alert configuration and report templates. Simulator records demonstrate UI and implementation surfaces, not real-device certification.',
'Development conditions are not product defects: no real SNMP devices, zero traffic, short history, offline test sensors and unconfigured channels are excluded from the feature-gap judgment. The previous revision gave these conditions excessive operational weight.',
'Remaining product gaps include topology lifecycle and routing depth, Nexus/ACI and cloud integrations, wireless client/RF visibility, F5 relationships, forecasting, dynamic baselines, a shared investigation timeline, compiled MIB tooling and syslog. Alert-evaluator source findings require reproduction; synthetic health discrepancies require a design/fixture review.',
'Plan: first confirm implementation gaps with deterministic fixtures and code tests, then build prioritized features. Real-device certification is a separate stage when equipment is available. The previous 8-12 / 20-28 week windows remain provisional scenarios pending this triage and test access, not revised commitments.',
]
METHODOLOGY.insert(0, 'Revision 2 / user clarification: this is a development appliance without live SNMP or real network devices. The original review recognized simulator records but did not consistently apply that limitation. This revision supersedes its operational-risk framing; it does not add new runtime tests.')
METHODOLOGY[-2] = 'Evidence limits: synthetic or empty data cannot establish collector failure, missing functionality, production outages or poor availability. No fault injection, real-device polling, delivery or failover test was performed. Source-backed omissions and explicit deferred features are distinguished from implementation that simply awaits testing.'
METHODOLOGY[-1] = 'Status counts measure evidence categories, not completion or parity percentages. Available / unproven and Unverified are validation items, not missing-feature counts. Real-device accuracy, sustained reliability and scale are reserved for later certification.'
STATUS_DEFS['Partial'] = 'Implemented with a specific feature-depth gap or source-backed semantic concern. Empty development data alone is not grounds for this status.'
STATUS_DEFS['Available / unproven'] = 'An implementation or workflow is visible, but behavior has not been exercised. Unconfigured or empty development state is not a defect.'

# Remove partial classifications whose sole basis was absent live validation.
for ident in ['F03','F05','F08','F09','F12','F13','F14','F22','F36','F48','F63','F65','F68']:
    ROWS[int(ident[1:])-1]['status'] = 'Available / unproven'
updates = {
'F01': 'Discovery wizard and backend exist. Empty development history is expected; validate discovery lifecycle with fixtures, then a controlled subnet when available.',
'F02': 'Recurring/cron and review/auto-match implementation exists. No configured development profiles is not a feature gap; test scheduler and import policy with controlled fixtures.',
'F04': 'Status/history surfaces are present. Development availability values are not a production SLA result; later test missing-check and denominator policy.',
'F05': 'v2c simulator records and v3 model/session support exist. AuthPriv/context interoperability needs real-device or vendor-virtual-device certification; no failure inferred.',
'F07': 'Vendor and standard metrics appear on simulator records. Real-device numerical accuracy remains a separate certification task.',
'F09': 'Historical charts and coverage disclosure exist. Short development history does not establish a retention or collection defect; test missing-data semantics and later sustained continuity.',
'F12': '64-bit counters and rate handling exist. Zero/dashed development throughput is expected without traffic; verify calculations with synthetic counter sequences, then known device traffic.',
'F18': 'Hardware panels exist. Synthetic faults alongside a 100/100 performance score raise a roll-up/labeling design question, not proof of failed physical sensors. Reproduce with coherent fixtures and agree score scope.',
'F30': 'ASA pack includes 19 metrics for connections, failover and remote-access counts. Zero attached development devices is not a gap; required per-tunnel/user drill-down depth remains to be established.',
'F36': 'Rich FortiGate SNMP panels and template implementation exist. Physical or virtual FortiOS validation is pending; simulator-only data does not reduce implemented coverage.',
'F37': 'Several vendor packs exist. Unassigned packs are not missing. Broader Arista/Extreme support was not established; assess family depth separately from model/firmware certification.',
'F45': 'History, routes, events and compare controls plus endpoints exist. A stale demo OK label is a fixture/freshness-policy question; reproduce on an enabled test probe before calling it a defect.',
'F46': 'Multiple-vantage NetPath execution was not verified. Offline development sensors do not establish lack of support; inspect capability wiring and test an isolated remote probe.',
'F57': 'Channel configuration, cooldowns and escalation controls exist. Zero destinations and disabled development gateways are expected configuration state. Test transport with local sinks before optional real delivery.',
'F60': 'v1/v2c receiver and feed exist; an empty development feed is not failure. v3 traps are explicitly deferred in source, independently of test traffic; verify required comparator scope before prioritizing.',
'F63': 'Availability views, report templates and coverage disclosure exist. Short synthetic history is not a product gap; validate denominator, maintenance and unknown-data policy with fixtures, then a real-device pilot.',
'F64': 'Fixed-section reporting and export/schedule controls exist. Arbitrary object/field/query authoring was not found in the inspected builder. Empty schedules/history are development configuration, not missing capability.',
'F66': 'LDAP/RADIUS implementation exists; lack of configured development authentication is not a defect. SAML/OIDC support was not established; track this scope separately from integration testing.',
'F68': 'Remote sensor deployment and assignments exist. An offline development sensor is not a distributed-polling defect; verify buffering, replay, timestamps and version compatibility in an isolated test.',
}
for ident,gap in updates.items(): ROWS[int(ident[1:])-1]['gap'] = gap
for ident in ['F01','F04','F07','F09','F12','F57','F63','F68']:
    ROWS[int(ident[1:])-1]['priority'] = 'Validation'
ROWS[17]['priority'] = 'P1 review'

FINDINGS = [
('Environment | Development conditions excluded from gaps', 'The user confirmed there are no live SNMP or real network devices on this appliance. Empty channels, short/synthetic history, zero throughput, no discovery runs and an offline test sensor are configuration/fixture conditions. They do not justify P0 product findings, a production outage claim or a restore-service workstream. Evidence E01-E04/E10/E13/E15.'),
('P0 candidate | Compound alert evaluation', 'Local source C04 mirrors conditions[0] into legacy fields; C03 reads compound fields but the reviewed periodic branches evaluate the legacy metric. CPU >90 AND memory >90 could therefore use only the first condition. This concern is independent of real SNMP availability. Verify deployed identity and reproduce with injected metric truth tables before confirming a defect.'),
('P0 candidate | Canonical state metric mapping', 'fan_state, psu_state, vpn_tunnel_state, ha_state and bgp_neighbor_down are accepted by the rule API but absent from the periodic canonical supported set. tpl_* is an alternative, so this is not absence of all component alerts. Trace accepted definitions through evaluation using fixtures; confirm or dismiss each mismatch. Evidence C03/C04.'),
('P0 candidate | Hold and dependency semantics', 'Reviewed template evaluation uses latest values in a lookback, which does not establish a continuous breach. Periodic SNMP paths do not show the dependency checks used by event-driven alerts. Test hold/reset/no-data and parent-child cases with a controlled clock and injected state. Confirm deployed behavior before labeling appliance defects. Evidence C03/C05.'),
('P1 review | Health score and stale demo semantics', 'Synthetic component faults alongside 100/100 and an old demo NetPath OK label are design/fixture questions. The score is described as CPU/memory/loss, so component faults need not imply a calculation bug. Agree labels, roll-up policy and demo behavior; reproduce using coherent enabled fixtures before requesting a fix. Evidence E02/E05/E08.'),
('Feature gaps | Explicit missing implementation remains relevant', 'Storage-only MIB uploads, absent exhaustion forecasts and the missing wireless-client/RF, cloud-controller, syslog and shared-timeline workflows do not depend on populated telemetry. Prioritize these based on source/UI evidence and target vendor scope. Automatic topology and vendor relationships have partial foundations rather than confirmed full coverage.'),
('Validation | Real-device interoperability remains unknown', 'Existing collectors and vendor packs cannot be certified from UI examples. Later compare SNMPv2c/v3, counters, traps, interface identity, hardware and protocol states against representative hardware or vendor virtual appliances. Lack of such access is a test prerequisite, not evidence that the features are missing.'),
('Validation | Reliability and scale require a separate stage', 'Coverage, notification delivery, collector continuity, access isolation, export accuracy, HA and capacity need dedicated tests. First use deterministic simulators and local test sinks; schedule real-device/scale qualification when available. These are future release criteria, not current development-appliance deficiencies.'),
]
EPICS[0].update(name='Build development validation and confirm findings', when='Weeks 1-2', deliver='Map deployed build to source; prepare deterministic SNMP/counter fixtures, a controlled test clock, local notification sinks and remote-probe fixtures. Reproduce alert candidates and agree health/freshness semantics. Catalogue implemented, missing and unverified behavior; size confirmed defects only.', accept='Repeatable scenarios cover known counter rates/resets, missing data, compound alerts, hold/recovery and simulated component faults. Results distinguish fixture behavior from product defects. No physical network equipment or production destination is required for this stage.')
EPICS[1]['depends'] = 'A: fixtures and representative model/firmware test plan'
EPICS[1]['deliver'] = 'Verify discovery/polling contracts with synthetic records; schedule real-device SNMPv3 and IPv6 interoperability separately. Test selection/reindex, wrap/reboot/discontinuity and freshness behavior. Implement missing MIB resolve/compile and custom-poller reuse workflows after checking existing depth.'
EPICS[2]['depends'] = 'A fixtures; code tracing and unit-level reproduction can start immediately'
EPICS[2]['deliver'] = 'Confirm the source candidates before changing code, then correct reproduced AND/OR, hold/reset, dependency and component-identity defects. Add capability validation, syslog workflow and required trap support. Exercise transports with local sinks during development.'
EPICS[2]['accept'] = 'Controlled tests cover supported metric families, parent-child suppression, independent tunnel state, hold/reset and replay idempotence. Synthetic traps/syslog reach local notification sinks. Real-device protocol and external-channel certification remains a later gate.'
EPICS[6]['depends'] = 'A synthetic history; B metric metadata; C events; real history for later forecast qualification'
EPICS[7]['depends'] = 'A-C verified; agreed target scale and later test infrastructure'
PLAN_NOTES = [
'Revision 2 separates development work from real-device certification. Weeks 1-2 establish deterministic fixtures and reproduce source candidates; there is no instruction to repair an idle development sensor or configure production channels as a product fix.',
'Previous estimates of 80-129 person-weeks, 8-12 weeks for qualified core scope and 20-28 weeks for broad enterprise scope are provisional scenarios, not validated revised estimates. Workstream A has changed scope. Re-estimate after fixture-based triage; untested behavior must not automatically become implementation work.',
'The earlier calendar scenario assumed about five effective delivery engineers plus part-time product/network ownership and timely access to equipment/APIs. Missing access leaves real-device certification unscheduled; the development calendar must not imply that certification has already been possible.',
'Use two tracks: implement source/UI-backed missing capabilities and reproduce candidate defects now; run model/firmware, delivery, multi-site, soak and recovery qualification when appropriate hardware, vendor virtual appliances and external systems are available.',
'The later real-device pilot may target 20 representative nodes across two sites, 100 interfaces and 14 days of evidence, followed by scale/soak testing. These are proposed acceptance targets to agree, not prerequisites for this development review or observed current deficiencies.',
'Keep NPM requirements, shared platform functions and separately licensed modules distinct. Prioritize target device families, versioned schemas and reversible releases. Confirm numerical accuracy and vendor compatibility separately from simulator-based semantic correctness.',
]
DECISIONS[0] = 'Which vendors, models and firmware are launch requirements, and which can be tested now using hardware, vendor virtual appliances or simulators?'
DECISIONS[3] = 'Which fixtures, mock notification sinks and test accounts can support development validation, and when can a separate real-device pilot begin?'
for key in ['E01','E02','E03','E04','E05','E07','E08','E10','E11','E13','E14','E15','E17']:
    name,path,note = EVIDENCE[key]
    EVIDENCE[key] = (name,path,note + ' Development context: this snapshot is not evidence of a production failure or missing functionality; configuration, fixture and implementation findings are separated in Revision 2.')
COUNTS = Counter(r['status'] for r in ROWS)
