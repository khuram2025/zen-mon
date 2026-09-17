# F5 BIG-IP monitoring assessment

Completed 18 September 2026. Live appliance deployment and verification performed against both discovered F5 devices, including the requested the assessed standby F5. Appliance timestamps currently report 17 September; certificate days remaining follow that clock.

## Finding and correction

The headline memory calculation selected the higher of TMM and non-TMM utilization. On the requested device, that produced about 91%, although total system RAM utilization was about 64%. The original 39 template OIDs were valid; the principal defect was interpreting one memory domain as overall memory.

The headline now uses `100 × sysGlobalHostMemUsed / sysGlobalHostMemTotal`. Separate widgets retain TMM, non-TMM and swap utilization. Invalid denominators, impossible ratios and mismatched collection timestamps are rejected by collector calculations. System RAM uses a new history series so the chart does not blend the old meaning with the corrected meaning. Existing historical evidence and alerts remain intact.

| Device | System RAM | TMM | Non-TMM | Swap occupancy |
|---|---:|---:|---:|---:|
| the assessed standby F5, standby | 64.07% | 21.97% | 90.58% | 94.43% |
| the assessed active F5, active | 48.73% | 10.50% | 76.91% | 20.75% |

These are verification snapshots, not permanent values. High swap occupancy alone does not establish ongoing paging or a memory leak.

## Comparison with established monitoring

| Reference | Relevant approach | Applied to ZenPlus |
|---|---|---|
| [F5 BIG-IQ memory model](https://clouddocs.f5.com/products/big-iq/mgmt-api/v7.0.0/ApiReferences/bigiq_public_api_ref/entity_catalog/bigip-memory.html) | Distinguishes total system, TMM, other memory and swap | Four explicitly labeled domains and matching numerator/denominator pairs |
| [Zabbix F5 integration](https://www.zabbix.com/integrations/f5) | Discovers CPU cores, disks, certificates, modules and load-balancing objects, alongside memory and traffic | Expanded table discovery, calculated disk utilization, expiry inventory, module provisioning, per-core CPU, pools and members |
| [Datadog F5 profile](https://github.com/DataDog/integrations-core/blob/master/snmp/datadog_checks/snmp/data/default_profiles/f5-big-ip.yaml) | Covers virtual servers, pools, nodes, connections and TCP/SSL failures | Added backend visibility, connection queues and failure rates |
| [Datadog CPU/memory profile](https://github.com/DataDog/integrations-core/blob/master/snmp/datadog_checks/snmp/data/default_profiles/_f5-big-ip-cpu-memory.yaml) | Its generic memory total/used mapping uses TMM values | Document the metric's basis explicitly; product percentages are comparable only when they measure the same domain |

This is an SNMP coverage comparison, not a claim of complete feature parity. HTTP application checks, active certificate binding, ASM/WAF request analytics, and configuration/API analytics need separate monitoring sources.

## Delivered coverage and presentation

The built-in F5 template now contains 78 OID definitions in 17 groups, up from 39 in 11 groups. It applies to both devices using this template.

- System RAM, TMM, non-TMM and swap percentages, with underlying byte values.
- Pool members: 302 rows per device; pool, node, port, availability, administrative state, connections and device-reported reason. Compound labels preserve membership in different pools.
- Nodes: 178 rows per device, health and connections.
- Disk partitions: 9 rows per device, used percentage and capacity calculated using reported block size.
- Certificate inventory: 20 objects per device, UTC expiration and days remaining.
- Module provisioning: 15 rows; unprovisioned modules are informational.
- Host CPU cores: 16 rows, utilization, I/O wait and stolen CPU.
- Existing 164 virtual servers and 188 pools retained, with additional ports, default pools, pool traffic, connections and queue data.
- SSL handshake, TCP accept and TCP connect failures shown as rates, not cumulative totals.
- Product, model, TMOS version, build, edition, hostname and HA synchronization detail.
- Empty physical sensor tables explained as unavailable; absence is not converted into zero or a healthy reading.

New large tables use five-minute collection intervals; disks use fifteen minutes, certificates and modules one hour. Counter rates require two samples after collector startup. Existing movable/resizable widgets and saved layout scopes are preserved.

## Operational findings requiring device-owner review

1. **the assessed standby F5 reports `/var` at 100% used through SNMP.** The `/var/named/lib` and `/var/dnscached/lib` entries report the same capacity/free values and may represent mounts of the same underlying filesystem. Verify directly on BIG-IP and investigate disk consumption before cleanup. `/usr` reports approximately 90.64% on both devices; interpret this in the context of the installed image/filesystem.
2. Non-TMM RAM remains approximately 91% and swap occupancy approximately 94% on the standby. The headline correction does not remove these readings. Investigate sustained trends and paging/process evidence separately.
3. Certificate inventory contains two expired objects and two objects expiring on 12 October 2026. Inventory presence does not prove any is currently bound to live traffic; confirm bindings and renewal status before concluding an application is affected.
4. Several virtual servers, pools, members and nodes report unavailable or administratively disabled states. The UI preserves the reported state and reason. These component states are separate from device reachability and SLA.
5. Verify appliance time synchronization; dates observed during this assessment differ from the workstation date. No clock changes were made.

Disk and certificate badges are presentation assessments, not newly configured notification rules. Existing generic memory alert rules now receive corrected overall system utilization; domain-specific alerting should use the explicit TMM/non-TMM/swap series.

## Validation and deployment

- All Go package tests passed; poller binary built successfully.
- Five focused Python tests passed for memory, disk, certificates, labels and non-F5 isolation.
- Template schema validation and migration idempotency passed.
- Existing dashboard layout/presentation/availability checks passed; no new TypeScript diagnostics against the appliance baseline; production dashboard build passed.
- Fresh live collections verified all new groups on both devices. ClickHouse generic `memory` and `f5_system_memory_pct` agree; failure and throughput rates populated after the next poll.
- Browser verified the requested device's four memory widgets, model, new metric tables, status summaries and rates.
- API, poller and nginx active after deployment.
- Backup: `/opt/zenplus/backups/f5-assessment-20260917T142944Z`.

## OID inventory

Names and numeric paths were checked against F5's SYSTEM and LOCAL MIB definitions: [SYSTEM MIB mirror](https://raw.githubusercontent.com/librenms/librenms/master/mibs/f5/F5-BIGIP-SYSTEM-MIB), [LOCAL MIB mirror](https://raw.githubusercontent.com/librenms/librenms/master/mibs/f5/F5-BIGIP-LOCAL-MIB). Live collection confirms compatibility with the observed BIG-IP 17.5.1.3 devices. Hardware availability depends on the platform.

| Group | Metric | OID | Type / unit |
|---|---|---|---|
| CPU (TMM vs Host) | TMM CPU (1m) | `1.3.6.1.4.1.3375.2.1.1.2.21.35.0` | gauge / % |
| CPU (TMM vs Host) | TMM CPU (5m) | `1.3.6.1.4.1.3375.2.1.1.2.21.36.0` | gauge / % |
| CPU (TMM vs Host) | Host CPU (1m) | `1.3.6.1.4.1.3375.2.1.1.2.20.29.0` | gauge / % |
| CPU (TMM vs Host) | Host CPU (5m) | `1.3.6.1.4.1.3375.2.1.1.2.20.37.0` | gauge / % |
| Memory | TMM Memory Used | `1.3.6.1.4.1.3375.2.1.1.2.1.45.0` | gauge / bytes |
| Memory | TMM Memory Total | `1.3.6.1.4.1.3375.2.1.1.2.1.44.0` | gauge / bytes |
| Memory | Non-TMM Memory Used | `1.3.6.1.4.1.3375.2.1.1.2.20.45.0` | gauge / bytes |
| Memory | Non-TMM Memory Total | `1.3.6.1.4.1.3375.2.1.1.2.20.44.0` | gauge / bytes |
| Memory | Swap Used | `1.3.6.1.4.1.3375.2.1.1.2.20.47.0` | gauge / bytes |
| Memory | Swap Total | `1.3.6.1.4.1.3375.2.1.1.2.20.46.0` | gauge / bytes |
| Memory | System RAM Total | `1.3.6.1.4.1.3375.2.1.1.2.20.2.0` | gauge / bytes |
| Memory | System RAM Used | `1.3.6.1.4.1.3375.2.1.1.2.20.3.0` | gauge / bytes |
| Connections & SSL | Client Connections | `1.3.6.1.4.1.3375.2.1.1.2.1.8.0` | gauge / conns |
| Connections & SSL | Server Connections | `1.3.6.1.4.1.3375.2.1.1.2.1.15.0` | gauge / conns |
| Connections & SSL | New Connections | `1.3.6.1.4.1.3375.2.1.1.2.1.7.0` | counter / conns/s |
| Connections & SSL | SSL Connections | `1.3.6.1.4.1.3375.2.1.1.2.9.2.0` | gauge / conns |
| Connections & SSL | SSL TPS (native) | `1.3.6.1.4.1.3375.2.1.1.2.9.6.0` | counter / tps |
| Connections & SSL | SSL TPS (compat) | `1.3.6.1.4.1.3375.2.1.1.2.9.9.0` | counter / tps |
| Connections & SSL | SSL Handshake Failures | `1.3.6.1.4.1.3375.2.1.1.2.9.29.0` | counter / failures/s |
| Connections & SSL | TCP Accept Failures | `1.3.6.1.4.1.3375.2.1.1.2.12.7.0` | counter / failures/s |
| Connections & SSL | TCP Connect Failures | `1.3.6.1.4.1.3375.2.1.1.2.12.9.0` | counter / failures/s |
| Throughput | Client In | `1.3.6.1.4.1.3375.2.1.1.2.1.3.0` | counter / bps |
| Throughput | Client Out | `1.3.6.1.4.1.3375.2.1.1.2.1.5.0` | counter / bps |
| Throughput | Server In | `1.3.6.1.4.1.3375.2.1.1.2.1.10.0` | counter / bps |
| Throughput | Server Out | `1.3.6.1.4.1.3375.2.1.1.2.1.12.0` | counter / bps |
| Virtual Servers | Availability | `1.3.6.1.4.1.3375.2.2.10.13.2.1.2` | enum /  |
| Virtual Servers | Enabled | `1.3.6.1.4.1.3375.2.2.10.13.2.1.3` | enum /  |
| Virtual Servers | Connections | `1.3.6.1.4.1.3375.2.2.10.2.3.1.12` | gauge / conns |
| Virtual Servers | Traffic In | `1.3.6.1.4.1.3375.2.2.10.2.3.1.7` | counter / bps |
| Virtual Servers | Traffic Out | `1.3.6.1.4.1.3375.2.2.10.2.3.1.9` | counter / bps |
| Virtual Servers | Detail | `1.3.6.1.4.1.3375.2.2.10.13.2.1.5` | string /  |
| Virtual Servers | Port | `1.3.6.1.4.1.3375.2.2.10.1.2.1.6` | gauge /  |
| Virtual Servers | Default pool | `1.3.6.1.4.1.3375.2.2.10.1.2.1.19` | string /  |
| Pools | Active Members | `1.3.6.1.4.1.3375.2.2.5.1.2.1.8` | gauge / members |
| Pools | Configured Members | `1.3.6.1.4.1.3375.2.2.5.1.2.1.23` | gauge / members |
| Pools | Availability | `1.3.6.1.4.1.3375.2.2.5.5.2.1.2` | enum /  |
| Pools | Detail | `1.3.6.1.4.1.3375.2.2.5.5.2.1.5` | string /  |
| Pools | Connections | `1.3.6.1.4.1.3375.2.2.5.2.3.1.8` | gauge / conns |
| Pools | Traffic In | `1.3.6.1.4.1.3375.2.2.5.2.3.1.3` | counter / bps |
| Pools | Traffic Out | `1.3.6.1.4.1.3375.2.2.5.2.3.1.5` | counter / bps |
| Pools | Connection queue | `1.3.6.1.4.1.3375.2.2.5.2.3.1.18` | gauge / conns |
| HA & Config Sync | Failover State | `1.3.6.1.4.1.3375.2.1.14.3.1.0` | enum /  |
| HA & Config Sync | Sync Status | `1.3.6.1.4.1.3375.2.1.14.1.1.0` | enum /  |
| HA & Config Sync | Failover Detail | `1.3.6.1.4.1.3375.2.1.14.3.2.0` | string /  |
| HA & Config Sync | Sync Detail | `1.3.6.1.4.1.3375.2.1.14.1.4.0` | string /  |
| Chassis Fans | Status | `1.3.6.1.4.1.3375.2.1.3.2.1.2.1.2` | enum /  |
| Chassis Fans | Speed | `1.3.6.1.4.1.3375.2.1.3.2.1.2.1.3` | gauge / RPM |
| Power Supplies | Status | `1.3.6.1.4.1.3375.2.1.3.2.2.2.1.2` | enum /  |
| Chassis Temperature | Temperature | `1.3.6.1.4.1.3375.2.1.3.2.3.2.1.2` | gauge / °C |
| System | TMOS Version | `1.3.6.1.4.1.3375.2.1.4.2.0` | string /  |
| System | Chassis Serial | `1.3.6.1.4.1.3375.2.1.3.3.3.0` | string /  |
| System | Product | `1.3.6.1.4.1.3375.2.1.4.1.0` | string /  |
| System | Hardware Model | `1.3.6.1.4.1.3375.2.1.3.3.1.0` | string /  |
| System | Build | `1.3.6.1.4.1.3375.2.1.4.3.0` | string /  |
| System | Edition | `1.3.6.1.4.1.3375.2.1.4.4.0` | string /  |
| System | System Hostname | `1.3.6.1.4.1.3375.2.1.6.2.0` | string /  |
| Pool members | Pool | `1.3.6.1.4.1.3375.2.2.5.6.2.1.1` | string /  |
| Pool members | Node | `1.3.6.1.4.1.3375.2.2.5.6.2.1.9` | string /  |
| Pool members | Port | `1.3.6.1.4.1.3375.2.2.5.6.2.1.4` | gauge /  |
| Pool members | Availability | `1.3.6.1.4.1.3375.2.2.5.6.2.1.5` | enum /  |
| Pool members | Enabled | `1.3.6.1.4.1.3375.2.2.5.6.2.1.6` | enum /  |
| Pool members | Connections | `1.3.6.1.4.1.3375.2.2.5.4.3.1.11` | gauge / conns |
| Pool members | Detail | `1.3.6.1.4.1.3375.2.2.5.6.2.1.8` | string /  |
| Nodes | Availability | `1.3.6.1.4.1.3375.2.2.4.3.2.1.3` | enum /  |
| Nodes | Enabled | `1.3.6.1.4.1.3375.2.2.4.3.2.1.4` | enum /  |
| Nodes | Connections | `1.3.6.1.4.1.3375.2.2.4.2.3.1.9` | gauge / conns |
| Nodes | Detail | `1.3.6.1.4.1.3375.2.2.4.3.2.1.6` | string /  |
| Disk partitions | Block size | `1.3.6.1.4.1.3375.2.1.7.3.2.1.2` | gauge / bytes |
| Disk partitions | Total blocks | `1.3.6.1.4.1.3375.2.1.7.3.2.1.3` | gauge /  |
| Disk partitions | Free blocks | `1.3.6.1.4.1.3375.2.1.7.3.2.1.4` | gauge /  |
| Certificate inventory | Expires (UTC) | `1.3.6.1.4.1.3375.2.1.15.1.2.1.4` | string /  |
| Certificate inventory | Expiry timestamp | `1.3.6.1.4.1.3375.2.1.15.1.2.1.5` | gauge / s |
| Provisioned modules | Provision level | `1.3.6.1.4.1.3375.2.1.11.1.2.1.2` | enum /  |
| Provisioned modules | Memory allocation ratio | `1.3.6.1.4.1.3375.2.1.11.1.2.1.3` | gauge / % |
| Provisioned modules | CPU allocation ratio | `1.3.6.1.4.1.3375.2.1.11.1.2.1.4` | gauge / % |
| Host CPU cores | CPU (1m) | `1.3.6.1.4.1.3375.2.1.7.5.2.1.27` | gauge / % |
| Host CPU cores | I/O wait (1m) | `1.3.6.1.4.1.3375.2.1.7.5.2.1.26` | gauge / % |
| Host CPU cores | Stolen CPU (1m) | `1.3.6.1.4.1.3375.2.1.7.5.2.1.38` | gauge / % |
