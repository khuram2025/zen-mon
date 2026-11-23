# ZenPlus 1.25.26

Alert rules can combine several conditions in one rule, for every kind of
asset ZenPlus watches, and raise one alert that names what matched.

Combined rules
- All of (AND), any of (OR), and one level of groups: for example
  "(CPU above 90% and memory above 90%) or service nginx stopped" on a server.
  Up to 10 conditions per rule.
- Servers: CPU, memory, swap, disks, filesystems, network errors, clock,
  services, processes and automatic services.
- Network devices: down, degraded, latency, CPU, memory, minutes SNMP has
  gone unanswered, and recent traps, syslog errors or configuration changes.
- Service checks: down, degraded, response time, certificate days.
- APM services: latency, error rate, throughput, Apdex.
- Virtualisation sources: host disconnected, datastore usage, platform alarms.
- The ZenPlus appliance: disk, databases, backups, updates, licence, clock,
  failed deliveries, remote sensors.
- A condition can read a linked asset: a server's device, a device's server,
  a service check's device or server, a virtualisation source's device. A
  linked asset that is missing or in maintenance never raises the alert.
- A condition can have its own "for at least" hold, besides the rule's hold.
- One alert per asset, naming the conditions that matched with their
  readings; the recovery says what everything reads now. Maintenance,
  exclusions, dependencies, snoozes, cooldown, schedules, priority and
  escalation work as for every other rule.

Editor
- Server rules: Add condition. Device, service check, APM, virtualisation and
  appliance rules: Combine conditions.
- Test against the last 7 days: replays the draft over stored history and
  lists when it would have fired, before you save.
- Recommended rules: Server under pressure and Device overloaded.

Also
- APM, Device Tracker, NetPath and service-status rules refuse a second
  condition: their evaluators used only the first one. Rules stored before
  stay editable.
- Service checks can be linked to the server they run on (API).

Notes for the upgrade
- Existing rules are unchanged and keep firing exactly as before.

Migrations 211-212. Appliance-only package; minimum upgrade base 1.24.36;
signed Windows agent catalog preserved. Details: PRs #194, #195 and the
phase 3 PR.
