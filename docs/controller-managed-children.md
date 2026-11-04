# Controller-managed children (FortiSwitch and FortiAP)

A FortiGate with **Promote managed devices** turned on materializes its
FortiLink switches and FortiAPs as child devices (`poll_mode =
'via_controller'`, migrate-069). A child is never pinged or polled and uses no
license seat. Everything shown for it is read through the FortiGate's own
SNMP poll: the child's row in the FortiGate's template tables
(`managed_source` + `managed_instance`) and the FortiGate's history series.

## What the child shows

| Where | Reading | Source on the FortiGate |
|---|---|---|
| Devices list CPU / Memory | latest controller reading, marked *via controller*, *stale* when older than the limit | `fgSwCpu` / `fgSwMemory`, `fgWcWtpSessionWtpCpuUsage` / `…MemoryUsage` |
| Devices list Availability (range) | share of controller-reported state samples that were up or degraded; *Insufficient history* below 90 % coverage | `fgSwDeviceStatus`, `fgWcWtpSessionConnectionState` history |
| Devices list Uptime (since boot) | FortiAP boot uptime; switches: not reported (see below) | `fgWcWtpSessionWtpUpTime` |
| Summary tiles + Controller panel | CPU, memory, availability (24 h), uptime since boot, clients or ports, controller link, measurement time, freshness, CAPWAP state or authorization, model, firmware, serial, IP, MAC | `fgSwDeviceTable`, `fgWcWtpSessionTable` |
| *Ports & PoE* tab (switch) | link, speed/duplex, native/allowed/untagged VLANs, PoE capability/state/draw, devices seen on each port, PoE-drawn and ports-up history | `fgSwPortTable` (+ Device Tracker MAC tables) |
| *Radios & clients* tab (AP) | per-radio band, PHY, width, channel, transmit power, clients, client signal summary; SSIDs per radio with clients and traffic; wireless clients; uplink (CAPWAP session, switch port); traffic and per-radio client history | `fgWcWtpSessionRadioTable`, `fgWcWtpSessionVapTable`, `fgWcWtpProfileRadioTable`, `fgWcStaTable` |
| Related panel | managing FortiGate, peers under the same controller, the switch port an AP is seen on / the APs on a switch's ports, each with status, model and last observation | `managed_by_device_id`, `udt_managed_locations` |

Rules:

* **Missing is not zero.** A child the controller reports offline shows no CPU,
  memory or clients ("not reported while offline"), and nothing is written to
  its history.
* **Stale is said.** A reading older than three controller polls of its group
  (at least 10 minutes) is marked stale; the child's own status decays to
  Unknown after 10 minutes unreported (unchanged).
* **Gaps are unknown.** A controller outage leaves a gap in the child's history
  and lowers coverage; it is never counted as the child being down.
  Maintenance windows are left out of availability entirely.
* **History stays with the child.** The managed-device sync writes each fresh
  CPU, memory and boot-uptime reading to the child's own `snmp_metrics` series
  (`poller_id = controller:<FortiGate id>`), so the Performance tab, reports
  and SNMP metric alert rules (CPU, memory, uptime reset) apply to the AP or
  switch, not the FortiGate. Status alerts keep the `controller` dependency
  edge, so a FortiGate outage is still one root-cause alert.
* **Client identity is permissioned.** Client MAC, IP and host name need
  `udt.view`; user names need `udt.view_users`. Counts, SSIDs and signal
  summaries do not.

## Template data model (migrate-171)

* `device_template_values.instance_oid` keeps the raw OID index suffix next to
  the hashed `instance` token, so a radio row `{vdom, wtpId, radioId}` can be
  related to its AP row `{vdom, wtpId}` and the AP serial and client MAC can
  be decoded from the index (those index columns are not-accessible).
* A metric with `"format": "hex"` stores a binary OctetString as hex
  (InetAddress, PhysAddress). The API decodes it to an IP or MAC.
* A table's `"max_rows"` (1–5000, default 500) bounds large controller
  tables (ports 4000, clients 2000).
* A group with `"display": "children"` is shown on the managed children, never
  on the FortiGate's own page, and declares how its rows belong to a child:
  `"belongs_to": {"group": "fortiswitch", "by": "serial", "metric": "fgt_swp_serial"}`,
  `{"by": "index_prefix", "layout": ["int","str","int"]}` or
  `{"by": "profile", "metric": "fgt_ap_profile", …}`.
* The `children` mapping names the child's `cpu_key`, `memory_key`,
  `uptime_key`, `clients_key`, `state_key`, `auth_key`, `ip_key`, `mac_key`,
  `serial_key` or `serial_from_index`.

Client identity columns are strings, so per-client rows never become
ClickHouse series.

## Required access

Everything above uses **read-only SNMP to the FortiGate** with
FORTINET-FORTIGATE-MIB (the SNMP profile the FortiGate already has).
FortiOS 6.4.2+ is needed for `fgSwDeviceTable`/`fgSwPortTable`, 7.0.1+ for
switch CPU and memory. Device Tracker's controller MAC collection (an NCCM
SSH profile on the FortiGate) supplies the AP-to-switch-port relationship;
without it the port is shown as not known.

## Not available from the FortiGate MIB

Shown as unavailable with the source that would supply it, never guessed:

| Reading | Read-only source that would supply it |
|---|---|
| Switch uptime since boot | FortiOS REST API `GET /api/v2/monitor/switch-controller/managed-switch/status`, or direct SNMP on the switch |
| Switch port traffic, utilization, errors, discards | FortiOS REST API `GET /api/v2/monitor/switch-controller/managed-switch/port-stats` |
| Switch PoE budget | FortiOS REST API `GET /api/v2/monitor/switch-controller/managed-switch/poe` |
| LAGs / trunks | FortiOS REST API `GET /api/v2/cmdb/switch-controller/managed-switch` (`ports[]` of type trunk and their members) |
| FortiSwitch LLDP neighbours | FortiOS managed-switch LLDP monitor, or LLDP-MIB on the switch |
| FortiLink uplink port | FortiOS REST API managed-switch status (`fgt_peer_intf_name`, ISL ports) |
| Radio channel utilization, retries, noise floor | FortiOS REST API `GET /api/v2/monitor/wifi/managed_ap` (`radio[]`) |

The REST API needs an API administrator with a read-only profile
(`wifi` and `switch-controller` read access) and a trusted host; it is not
collected in this release. Verify the field names against the FortiOS API
reference of the build in use before relying on them.
