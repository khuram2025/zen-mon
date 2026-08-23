# SNMP Network and Security Device Coverage

This document is the release contract for SNMP coverage. “Supported” means the
appliance uses published MIB objects, tolerates unsupported objects without
failing the poll, bounds table size/cadence, and retains stable canonical CPU
and memory metric keys. It does not mean every object in every vendor MIB is
polled.

## Universal network coverage

Routers, switches, firewalls, and access points receive the following standard
collectors whether or not a vendor profile is attached:

| Capability | MIB | Data | Cadence |
|---|---|---|---:|
| Identity/uptime | SNMPv2-MIB | description, object ID, name, contact, location, uptime | device poll |
| Interfaces | IF-MIB / IF-X-MIB | state, speed, 32/64-bit traffic, errors, discards | device poll |
| Hardware inventory | ENTITY-MIB | chassis/modules, model, serial, revisions | device poll |
| Sensors | ENTITY-SENSOR-MIB | temperature, fan, voltage, current, power, percent, operational status | device poll |
| Neighbors/topology | LLDP-MIB, CISCO-CDP-MIB | remote chassis/port/system and topology links | UDT cadence |
| Endpoint/VLAN tracking | BRIDGE-MIB, Q-BRIDGE-MIB, IP-MIB | FDB, VLAN, ARP/ND bindings | UDT cadence |
| BGP | BGP4-MIB | peer state/admin state, remote AS, established time | 120 s |
| OSPF | OSPF-MIB | neighbor state, changes, retransmit queue | 120 s |
| First-hop redundancy | VRRP-MIB / VRRPV3-MIB | IPv4/IPv6 router state, priority, master uptime | 120 s |
| Spanning tree | BRIDGE-MIB | topology changes, root/port state and cost | 120–300 s |
| Link aggregation | IEEE8023-LAG-MIB | actor/partner key and selected/attached aggregator | 300 s |

Vendor profiles take precedence for the canonical health headline. Duplicate
standard groups are automatically omitted when a vendor profile already walks
the same table. Each table is capped at 500 rows.

## Deep vendor profiles

| Vendor/platform | Deep coverage |
|---|---|
| Cisco IOS/IOS-XE | PROCESS CPU, 32/64-bit memory pools, ENVMON temperature/fan/PSU, StackWise, PoE |
| Cisco ASA | connections, PROCESS CPU, enhanced 64-bit memory, failover, remote-access VPN |
| Fortinet FortiGate | global/data-plane CPU and memory, sessions, VDOM, HA, VPN, SD-WAN, FortiAP/FortiSwitch, sensors |
| Palo Alto PAN-OS | HOST-RESOURCES processors/memory, sessions, HA, GlobalProtect, vsys, content versions |
| Juniper JunOS/SRX | operating-component health, alarms, RE redundancy, SPU/session load, BGP |
| Aruba controllers | controller CPU/memory, role, AP/client totals, AP state, fan/PSU |
| Aruba AOS-CX | module CPU/memory/storage/self-test, VSF role/state/identity/utilization |
| F5 BIG-IP | TMM/host CPU, separate TMM/host memory domains, pools, virtual servers, HA/device groups |
| Dell SmartFabric OS10 | processor load and cache-aware available-memory calculation |
| MikroTik RouterOS | HOST-RESOURCES CPU/memory, RouterBOARD temperature/power/PSU/fans, PoE, optics, IPsec IKE SAs |

CPU for multi-processor/chassis systems is the highest valid component load,
not an average that can conceal one saturated member. Memory domains are never
summed. F5 TMM and host memory remain separate; Cisco memory pools remain
separate; Dell OS10 uses available memory so filesystem cache is not counted as
an outage-level utilization.

## Identification coverage and next profile candidates

Discovery identifies Cisco, Juniper, Fortinet, MikroTik, Aruba, Palo Alto,
Arista, F5, Huawei, Check Point, A10, Citrix ADC, Extreme, Ruckus, Ubiquiti,
TP-Link, Dell/Force10, Netgear, Brocade, and Zyxel enterprise trees. A detected
vendor without a deep profile still receives universal standards-based
monitoring. A new proprietary pack must be added only after its objects and
units are verified against a current vendor MIB/reference and a device walk.

Priority candidates for future deep packs are Arista EOS, Huawei VRP, Check
Point Gaia, Ubiquiti/UniFi, A10 ACOS, and Citrix ADC. These are
not represented as deep-supported until validated against real hardware.

## Primary references

- Aruba AOS-CX 10.12 SNMP/MIB Guide: <https://www.arubanetworks.com/techdocs/AOS-CX/10.12/PDF/snmp_mib.pdf>
- Dell OS10 cache-aware memory guidance: <https://www.dell.com/support/kbdoc/en-us/000270359/dell-networking-smartfabric-os10-identifying-memory-utilization-in-os10>
- Palo Alto HOST-RESOURCES support: <https://docs.paloaltonetworks.com/pan-os/11-1/pan-os-admin/monitoring/snmp-monitoring-and-traps/supported-mibs/host-resources-mib>
- Cisco CPU monitoring: <https://www.cisco.com/c/en/us/support/docs/ip/simple-network-management-protocol-snmp/15215-collect-cpu-util-snmp.html>
- FortiGate 8.0 MIB overview: <https://docs.fortinet.com/document/fortigate/8.0.0/fortigate-mib-information-overview/293724>
- Juniper SNMP health monitoring: <https://www.juniper.net/documentation/us/en/software/junos/network-mgmt/topics/topic-map/health-monitoring-with-snmp.html>
- MikroTik SNMP and current MIB download: <https://help.mikrotik.com/docs/spaces/ROS/pages/8978519/SNMP>
- BGP4-MIB: <https://www.rfc-editor.org/rfc/rfc4273.html>
- OSPF-MIB: <https://www.rfc-editor.org/rfc/rfc4750.html>
- VRRPv3 MIB: <https://www.rfc-editor.org/rfc/rfc6527.html>
