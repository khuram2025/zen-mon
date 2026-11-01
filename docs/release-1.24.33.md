# ZenPlus 1.24.33 — Grouped server events and lifecycle alerts

Windows server events now explain what happened: source/provider, Event ID,
message, affected component when supplied, first/last occurrence and repeat
count. Expand a group for event timestamps, record IDs and structured fields.
Filtering, search and pagination run on the appliance. Counts use unique event
identities; overlapping policies, upload retries and log record-ID reuse do
not inflate them. Legacy summaries remain available separately.

## Collection and traffic controls

- Agent 1.14.4 advertises `event_details_v1`; older agents retain their existing
  collection contract. Upgrade agents to receive detailed history.
- Agent Policies offers General, Domain controller/DNS, IIS and SQL Server
  profiles. Custom filters override presets. Audit events have their own
  Level 0 selection; the DC preset excludes successful-logon traffic.
- Defaults: collect every 60 seconds, at most 200 details / 256 KiB per cycle,
  and 2 KiB per message. Limits are bounded and configurable. Uploads use the
  existing negotiated gzip transport and controller backpressure.
- Native Windows bookmarks and queued batches commit in one local database
  transaction. Full queues stop checkpoint advancement instead of deleting
  unacknowledged detailed events. Remaining source events wait for the next
  collection. Lifecycle events have a separate collection allowance.
- Collection status distinguishes current, stale, backlog, query errors and
  cleared/rolled log gaps. Source logs can roll during long outages; this is
  reported as a coverage gap. Detailed history expires after 14 days.

## Restart, shutdown and possible hang notifications

Two enabled global host rules are seeded once: Server restart and Server
shutdown. They use existing notification channels, priority/escalation routes,
schedules and silences. Operators select notification recipients in Alert
Manager; an appliance without a configured delivery route cannot send mail/SMS.
Repeated lifecycle evidence shares one incident over a 15-minute observation
window. Historical backfill does not page. Linked-device maintenance suppresses
the corresponding lifecycle evidence, including after maintenance ends.

OS boot-time changes also support restart detection on agent-monitored Linux
servers. Restarting the agent or the Windows Event Log service is not treated
as an OS restart. Windows classification checks the provider as well as the
Event ID. Shutdown requests and actual OS boots are described separately.

An orderly shutdown can stop the agent before an upload; shutdown evidence
may therefore arrive after the next boot. Abrupt power loss is detected from
availability while offline and unexpected-shutdown evidence after boot. The
existing Agent offline rule reports a silent agent on a pingable server as
unresponsive / possible agent failure or system hang, with cause unconfirmed;
Device Down covers unreachable hosts. No remote reboot is performed to test
these conditions.

Appliance migrations 169–170 add event collection state, bounded lifecycle history,
the new alert metrics and a ClickHouse detail table. Minimum upgrade base is
1.23.9. Appliance and agent versions are independent: 1.24.33 / 1.14.4.
