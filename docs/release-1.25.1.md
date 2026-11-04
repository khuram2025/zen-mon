# ZenPlus 1.25.1

FortiGate-managed FortiSwitches and FortiAPs now show the readings their
controller already collects. The Devices list shows their CPU and memory,
marked as reported via the controller and flagged when stale. Availability
(range) and Uptime (since boot) are separate columns. A child's availability
comes from controller-reported state and shows "Insufficient history" when
the controller observed less than 90% of the range. Child pages show the
controller, measurement time, freshness, CAPWAP state or authorization,
firmware, serial, IP, MAC and boot uptime. Switches get a Ports & PoE tab
(link, speed, VLANs, PoE draw, APs seen on each port, history). APs get a
Radios & clients tab (band, channel, power, clients and signal per radio,
SSIDs, wireless clients behind Device Tracker permissions, uplink switch port,
history). Related lists the managing FortiGate, peers and controller-known
neighbours. Readings the FortiGate MIB does not carry are listed as
unavailable, with the read-only FortiOS API that would supply them. Children
are still not polled directly and use no license seat. Their CPU, memory and
uptime history now belongs to the child, so device metric alert rules
without a device scope also evaluate them.

NCCM: fresh installs and upgrades install, enable and verify the NCCM worker
and timers. The readiness view tells "not installed" apart from "inactive".
SSH-key connection profiles work with the bundled Netmiko 4.7.0 and report
the exact key-loading cause. The capture deadline is configurable per
connection profile and device (FortiOS default 900 s). Captures report live
progress, and paginated FortiOS reads are faster. Complete-capture validation
and encrypted storage are unchanged.

Includes 1.25.0 (native virtualization monitoring). Migrations 182 (controller child readings, Fortinet template extension) and
183 (NCCM capture deadline and progress). Appliance-only release requiring
the signed 1.24.36 bridge; existing Windows agent packages are preserved.
