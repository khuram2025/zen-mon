# ZenPlus 1.25.20

Alerting overhaul from the 19 November 2026 audit: far less noise, clearer
messages, and alerts for what was not covered before. Nothing is created or
changed in your thresholds by the upgrade.

Less noise
- Latency and packet-loss rules fire only when a device is Degraded. A device
  that stops answering is reported once, by Device Down, after its hold.
- Server health (CPU, memory, disk colour on the server pages) no longer opens
  alert rows of its own; the server alert rules cover the same limits with a
  hold and a recovery.
- "Flapping started/stopped" notices stay in the alert list; sending them is a
  switch under Escalation & On-call > Alert storms.
- The hold of the three oldest built-in rules (Device Down, High Latency,
  Packet Loss) now takes effect where their timers were never changed.
- Traps, syslog, Device Tracker events and NCM changes keep one open alert per
  condition and close after 7 days without a new event.
- Server and APM rules honour their cooldown (a trigger is delayed, never
  dropped). Servers behind a device that is down are not paged.
- Maintenance windows can repeat weekly and can cover servers.

Clearer messages
- SMS use the plain GSM alphabet and fit one message in most cases.
- Messages name the rule's own condition, the site and the incident, say
  "Degraded for" or "Down for", and use one date style. Every email links to
  the incident. Chat and PagerDuty titles are the event sentence.
- What each notification said is kept with its delivery for 30 days.

New alerts
- The appliance itself: data disk, ClickHouse, backups, updates, licence,
  clock, failing notification channels, remote sensors, sign-in failures and
  failed reports (seeded and enabled; administrative changes seeded off).
- Certificate expiry, service response time, F5 certificates.
- Hardware and protocol state through device templates: power supplies, fans,
  HA, VPN tunnels, BGP peers, F5 pools; temperature per sensor.
- Virtualisation hosts, datastores and alarms; NCM compliance violations and
  unsaved configuration.
- SNMP failing while ping answers; agents that send no metrics.
- Interface error and discard rate, flapping, speed changes.
- Server swap, disk latency, NIC errors, clock skew, days until a disk is
  full, an automatic service that stopped with an error. Linux servers now
  report services, load and restarts.
- Recommended rules (Alert rules > Recommended) can be accepted one by one.

Also in this release
- Discovery rules and dynamic groups: a server inventory list (services,
  watched processes, software) not refreshed for 24 hours counts as "not
  reported", so a server whose agent stopped leaves groups built on it (#186).
- System status: the chart shows the first minutes of history (#183).

Notes for the upgrade
- Existing temperature rules now evaluate each sensor and may start alerting.
- Open trap, syslog and Device Tracker event alerts older than 7 days close
  silently after the upgrade.
- Webhook channels receive a documented field set (no recipient addresses);
  NCM webhooks use the standard payload.

Migrations 196-199, 201-203, 205-208. Appliance-only package; minimum upgrade
base 1.24.36; signed Windows agent catalog preserved. Details:
docs/alert-notification-policy.md and the docs it links.
