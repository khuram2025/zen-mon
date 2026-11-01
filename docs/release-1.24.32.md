# ZenPlus 1.24.32 — Server event logs, server alert rules, custom time ranges

## Windows Event Log is collected again

Windows servers stopped sending Event Log summaries the day they enrolled, so the server Events tab stayed empty. Agent policies stored an empty channel list, and the agent read it as "watch no channels".

- **Default channels.** A policy without its own channels now sends the defaults: System and Application, at Critical, Error and Warning. Agents pick this up at their next check-in, with no agent upgrade needed. Event history from the gap cannot be recovered.
- **Agent Policies.** A new Windows Event Log section has an on/off switch and a channel list (`Channel: levels | event IDs`; leave it blank for the defaults). Channels the agent would reject are refused when you save. The policy list shows each policy's event-log setting.
- **Events tab.** It now follows the page's time range, including custom ranges. An empty period explains what is collected instead of pointing at a switch that did not exist.

## Server alert rules in Alert Manager

- **Server tab.** The alert wizard has a Server tab for CPU, memory, filesystem, disk I/O, service stopped, process not running and Agent offline, scoped to all servers or one server.
  - Editing a server rule (for example the seeded Agent offline rule) opens it on this tab. Before, it opened as a "Ping status" device rule and could not be saved.
  - A deleted Agent offline rule can be recreated here.
- **Rule list.** Server rules are described in words ("Agent silent for 3 min, server still answers ping"), the Scope column names the server, and Duplicate works for server rules.
- **Device Down.** When it applies to all devices, the wizard notes that it also covers agent servers.
- **Schedules.** Setting a schedule ("Notify from/until") on any alert rule no longer fails to save.
- **Wording.** The alert wizard no longer mentions other products.

## Custom time ranges

- **No future ranges.** The custom range picker no longer accepts a start time in the future: a day changed without the month used to ask for a date weeks ahead. An end time after now stops at now, and the chosen range is shown with the month spelled out.
- **Empty server Performance tab.** It now says when a range is in the future, or when no metrics were stored for that period, instead of blaming the agent's monitoring profile.

Appliance-only release, minimum version 1.23.9. No database migration.
