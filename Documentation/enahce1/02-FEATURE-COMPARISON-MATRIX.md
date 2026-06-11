# ZenPlus — Feature Comparison Matrix (vs. 15 competitors)

> `F` = full · `P` = partial · `–` = none · `?` = unknown. **ZEN = ZenPlus (us).** `#F` = how many of the 15 competitors have the feature *fully* (a table-stakes signal). Competitor scores are from 2024–2026 web research; ZenPlus from the live app + code audit.

**Competitor key:** SW SolarWinds · DD Datadog · ZBX Zabbix · DT Dynatrace · PRTG Paessler PRTG · LM LogicMonitor · AUV Auvik · ME ManageEngine OpManager · NAG Nagios/Icinga · CMK Checkmk · LNMS LibreNMS/Observium · TE ThousandEyes · KTK Kentik · GRAF Grafana stack · DOM Domotz/NinjaOne

| Feature | ZEN | SW | DD | ZBX | DT | PRTG | LM | AUV | ME | NAG | CMK | LNMS | TE | KTK | GRAF | DOM | #F |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **A. Discovery & Inventory** | | | | | | | | | | | | | | | | | |
| Auto-discovery | **P** | F | F | F | F | F | F | F | F | P | F | F | P | F | P | F | 12 |
| L2/L3 topology map | **P** | F | P | P | P | P | F | F | F | P | P | P | P | P | – | F | 5 |
| Inventory/asset | **F** | F | P | P | P | P | P | F | F | P | F | P | P | P | – | F | 5 |
| HW health (SNMP) | **F** | F | F | F | F | F | F | F | F | F | F | F | P | F | P | F | 13 |
| **B. Availability & Performance** | | | | | | | | | | | | | | | | | |
| ICMP ping | **F** | F | P | F | F | F | F | F | F | F | F | F | F | F | P | F | 13 |
| SNMP polling | **F** | F | F | F | F | F | F | F | F | F | F | F | P | F | P | F | 13 |
| SNMP traps | **P** | F | F | F | F | F | F | – | F | F | F | P | – | F | – | P | 10 |
| Iface bandwidth/err | **F** | F | F | F | F | F | F | F | F | F | F | F | P | F | F | F | 14 |
| Service/port checks | **F** | P | F | F | F | F | F | P | F | F | F | P | F | P | F | P | 10 |
| Synthetic/journeys | **–** | F | F | F | F | P | P | – | P | P | F | – | F | F | F | – | 8 |
| Streaming telemetry (gNMI) | **–** | – | – | P | ? | ? | – | – | F | – | – | – | P | F | P | – | 2 |
| **C. Flow & Traffic** | | | | | | | | | | | | | | | | | |
| Flow ingest | **F** | F | F | – | P | F | F | F | F | P | P | P | P | F | P | P | 7 |
| Top talkers/convos | **F** | F | F | – | P | F | F | F | F | P | P | P | F | F | P | P | 8 |
| DPI/app recognition | **P** | P | P | – | P | P | P | F | F | – | P | – | P | P | – | P | 2 |
| Traffic capacity/forecast | **P** | F | P | P | P | P | P | P | F | P | P | P | P | F | P | P | 3 |
| **D. Network Config Mgmt** | | | | | | | | | | | | | | | | | |
| Config backup/diff | **P** | F | P | P | – | P | F | F | F | – | – | P | – | F | – | F | 6 |
| Config push/automation | **–** | F | – | P | – | – | P | – | F | – | – | – | – | – | – | P | 2 |
| Compliance/FW-EoL/CVE | **–** | F | – | P | – | – | P | P | F | – | P | – | – | – | – | P | 2 |
| **E. Maps & Visualization** | | | | | | | | | | | | | | | | | |
| Topology maps (edit) | **P** | F | P | P | P | P | F | F | F | P | P | P | P | P | P | F | 5 |
| Geo maps | **F** | F | F | F | F | F | F | P | F | P | P | F | F | F | F | – | 11 |
| Custom dashboards/NOC | **P** | F | F | F | F | F | F | P | F | F | F | P | F | F | F | P | 12 |
| Service/dependency map | **P** | P | F | F | F | P | P | P | P | F | F | P | P | P | P | P | 5 |
| **F. Wireless / SD-WAN / Cloud / Internet** | | | | | | | | | | | | | | | | | |
| Wireless/AP/heatmap | **–** | P | P | P | P | P | P | P | F | P | P | P | P | – | P | P | 1 |
| SD-WAN | **–** | P | P | – | P | P | F | P | F | – | – | – | F | P | – | – | 3 |
| Cloud infra (AWS/Az/GCP) | **–** | F | F | F | F | F | F | P | F | P | F | – | P | F | F | P | 10 |
| Internet/WAN path/BGP | **–** | F | F | P | P | P | P | P | F | – | P | P | F | F | P | P | 5 |
| **G. Alerting & Incident** | | | | | | | | | | | | | | | | | |
| Multi-condition rules | **P** | F | F | F | F | F | F | F | F | F | F | F | F | F | F | F | 15 |
| Anomaly/ML baseline | **P** | P | F | P | F | P | F | P | F | – | P | – | F | F | P | – | 6 |
| Dependency/root-cause | **P** | F | F | F | F | P | F | P | F | F | F | P | P | P | P | F | 9 |
| Dedup/correlation | **P** | P | F | P | F | P | F | P | P | P | F | P | P | P | P | P | 4 |
| Maintenance windows | **F** | F | F | F | F | F | F | F | F | F | F | F | ? | ? | F | F | 13 |
| On-call/escalation | **–** | P | F | P | P | P | F | – | F | F | P | P | – | P | F | P | 5 |
| Notification channels | **F** | F | F | F | F | F | F | F | F | F | F | F | F | F | F | F | 15 |
| Incident/ITSM | **P** | P | F | P | F | P | F | P | F | P | P | P | P | P | F | F | 6 |
| **H. Servers / Agents / APM / Logs** | | | | | | | | | | | | | | | | | |
| Server/host agent | **P** | F | F | F | F | F | F | F | F | F | F | P | – | – | F | F | 12 |
| App/DB/container mon | **–** | F | F | F | F | P | F | P | F | P | F | P | P | – | F | P | 8 |
| Logs/SIEM | **–** | F | F | P | F | P | P | P | P | P | P | P | – | – | P | P | 3 |
| APM/traces/RUM | **–** | P | F | – | F | – | F | – | P | – | P | – | – | – | F | – | 4 |
| K8s/containers | **–** | P | F | F | F | P | F | – | P | P | F | – | – | F | F | – | 7 |
| **I. Analytics / Reporting / AIOps** | | | | | | | | | | | | | | | | | |
| Reports+export | **F** | F | P | F | F | F | P | F | F | F | P | P | F | F | P | F | 10 |
| SLA/uptime | **F** | F | F | F | F | F | F | P | F | F | F | P | P | P | F | P | 10 |
| Capacity planning | **P** | F | P | P | P | P | F | P | F | P | F | P | P | F | P | P | 5 |
| AI/ML/AIOps/GenAI | **P** | P | F | P | F | P | F | F | F | – | P | – | F | F | P | P | 7 |
| Exec/business dash | **F** | P | F | P | F | P | P | P | P | P | P | – | P | P | P | P | 2 |
| **J. Platform & Operations** | | | | | | | | | | | | | | | | | |
| RBAC/SSO/multitenant | **P** | F | F | F | F | P | F | F | F | F | F | P | F | F | F | F | 13 |
| Scalability/distributed | **P** | F | F | F | F | P | F | F | F | F | F | F | F | F | F | P | 13 |
| HA/failover | **–** | F | F | F | F | F | F | P | F | F | F | P | P | F | F | ? | 11 |
| REST API/IaC | **P** | F | F | F | F | P | F | F | P | F | F | P | F | F | F | F | 12 |
| Integrations ecosystem | **P** | P | F | F | F | P | F | F | F | F | F | P | F | F | F | F | 12 |
| Deployment options | **P** | F | P | P | F | F | P | P | F | P | F | P | P | P | F | P | 6 |
| Mobile app | **–** | P | F | F | P | F | F | ? | F | P | P | F | P | – | F | F | 8 |
| Pricing transparency | **P** | P | P | P | P | P | P | P | P | F | P | F | P | P | P | P | 2 |
