# ZenPlus 1.24.31 — Servers that go down now page

A Windows server monitored only by its agent went down for about 80 minutes overnight and nobody was paged. "Device Down" only watched ping-monitored devices, and the "Agent offline" alert raised 10 minutes later had no rule, so it had no email channel and no escalation policy.

- **Device Down now covers servers too.** A Device Down rule that is not limited to a device, group, type or location now also pages for any agent-monitored server whose agent goes silent and that ping cannot show as up. It uses the rule's own channels, priority and escalation policy. This works for rules set to "Goes down" and for the default "Any change" rule with `ping_status = 0`. A tag scope filters servers the same way it filters devices. The Alert Rules page shows these rules as **All + servers**.
- **New "Agent offline" rule (warning, P3).** It fires when a server's agent stops reporting but the server still answers ping: monitoring is lost, but the server is not down. It follows the P3 default escalation policy. It is also available when creating a server rule.
- **Faster detection.** A server is treated as gone after 3 minutes without a heartbeat, instead of 10. A rule's "for" duration overrides this, down to a minimum of 90 seconds.
- **No double alerts.** If a server's linked ping device is down, the device alert handles it. A device in maintenance pages nobody. The old built-in "agent offline" alert, which had no route, now appears only for a server that no rule covers, and a rule alert closes it.
- **Upgrading does not page old outages.** Servers that were already silent before this version started (lab machines, decommissioned hosts) are recorded but not emailed. Their next outage pages as usual.

Schema: migration 168 adds the `host_agent_offline` metric and seeds the Agent offline rule.

Appliance-only release, minimum version 1.23.9.

## Security

- **Dashboard dependency update.** `follow-redirects` (used by `axios`) is updated from 1.16.1 to 1.16.2 for a high-severity advisory (GHSA-mrwp-f3q8-3g9p): custom credentials could follow redirects. Only the lockfile changes.

Version 1.24.30 was tagged but never built or published; its changes ship in this release.
