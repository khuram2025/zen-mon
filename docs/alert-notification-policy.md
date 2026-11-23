# Alert notification policy: metric incidents, delivery and reports

This page describes how ZenPlus turns metric readings into incidents and
notifications, and how it guarantees that one incident event is sent once. It
answers a field review of 196 notifications over 48 hours (October 2026). In that
review, 121 memory warnings came from readings between 90.1% and 91.5% against a
90% rule. Interface-error emails arrived twice, seconds apart. Daily reports
titled 08:00 Riyadh ran at 05:00 Riyadh.

## Noise fixes (1.25.18)

From the alerting audit of 19 November 2026 (development and one customer appliance, 30
days of history):

* **Latency and loss rules own Degraded.** A move into Down carries no round-trip time and
  no packet-loss reading. A "packet loss > 5%" rule with trigger *Any change* used to match
  every outage at once (as 100% loss), ahead of the held Device Down alert: 966 incidents,
  none of them real loss, 905 emails on one appliance. Only reachability rules
  (`ping_status`) fire on Down now.
* **Server health is a colour, not an alert.** The built-in "health_threshold" rows opened on
  one agent upload above a fixed limit and closed on the next one below it (58% of all alert
  rows on that appliance). They are retired; open ones are closed administratively
  (migrate-196). The host alert rules (High / Critical CPU, memory, filesystem) cover the same
  limits with a hold, a clear level and a recovery.
* **Flapping notices stay in-app by default** (see `docs/device-status-policy.md`).
* **Shipped holds take effect.** The three oldest shipped rules carried their hold in
  `alert_rules.duration`, which no evaluator reads. Where their timers and condition are still
  exactly as shipped, migrate-196 moves it into `min_duration` (Device Down 180 s, 120 s on
  new installs; High Latency and Packet Loss 300 s) and turns the recovery notice on for a
  Device Down without a channel. A rule whose timers anyone set is left alone.
* A dependency-suppressed alert is recorded as not notified, so it no longer starts the
  rule's cooldown and swallows the first real page after the parent recovers.
* A service check that fails during a maintenance window is sent to the alert engine when the
  window ends (and one that recovered during it closes its earlier alert).
* Device and server maintenance windows may recur weekly (for example nightly backups at
  02:00 local time) and may cover servers; every evaluator asks the one SQL definition of "in
  force", and the end of an occurrence behaves like the end of a one-off window
  (`docs/maintenance-windows.md`).
* Repeated configuration changes on one device keep one open NCM alert (with a count); every
  change is still notified and kept in the NCM history.

## Metric incidents (servers and SNMP devices)

One incident per **rule + asset + dimension** (a filesystem incident is per
mount; an interface incident per interface). Its lifecycle is:

1. **Breach** — server CPU, memory and disk utilisation rules compare the
   rolling average over *Condition must exist for* (`min_duration`, at least
   2 minutes), as before. One brief dip does not reset a sustained breach.
   Filesystem and SNMP rules require the condition to persist for
   `min_duration` on every sample before the incident opens and notifies.
2. **Clear threshold (hysteresis)** — the incident clears at the rule's reset
   level. Percent rules (CPU, memory, filesystem, disk utilisation) without an
   explicit reset level clear 2 points on the healthy side: a `> 90%` rule clears
   at or below 88%. The breach threshold itself is never changed.
3. **Stable recovery** — the cleared condition must hold for *Clear after*
   (`clear_duration`, default 300 s; 0 resolves on the first healthy sample).
   A new breach during that time continues the same incident; no new email.
4. **Missing or stale samples never resolve** an incident and never count as
   clear time; the incident is marked as having no current data.

Certificate expiry (`service_tls_days`, `f5_cert_days`) and service response time
(`service_response_ms`) follow the same lifecycle in their own evaluator; their
default clear level is 1 day or 10 % on the healthy side (see
[service-metric-alerts.md](service-metric-alerts.md)).
Monitoring gaps (`snmp_unreachable`, `agent_no_metrics`) do too; they clear only
once data returns (see [monitoring-gap-alerts.md](monitoring-gap-alerts.md)).

**Hardware and protocol state** (1.25.18) is alerted through device-template
state metrics: each tunnel, power supply, fan, HA member, BGP peer or pool
member is its own dimension, and the message names it from the template — row
label, the metric's display name and the value label — never the series key or
the numeric code: "IPsec tunnel Branch-12 on Firewall FGT-01 (10.0.0.1) is
Down.", recovery "… is Up again.". The email headline shows the metric name and
the state; the details table lists the component. Templates recommend these
rules (see [alert-rule-overrides.md](alert-rule-overrides.md#state-rules));
`temperature` rules are per sensor.

Virtualisation rules (`vm_host_disconnected`, `vm_datastore_pct`, `vm_alarm`)
follow it too, in `virtualization_alert_service`; datastore usage clears 2
points on the healthy side by default (see
[virtualization-alerts.md](virtualization-alerts.md)). Rule-less NCM alerts
(configuration change, backup failure, compliance, unsaved configuration) keep
their durable outbox but are sent through `dispatch_to_channels` with the alert
id and delivery event (see [ncm-change-detection.md](ncm-change-detection.md)).

### Server rules (host_alert_service)

Server (agent) rules follow the lifecycle above. In addition:

* **Combined rules** (migrate-211, see
  [multi-condition-rules.md](multi-condition-rules.md)): a rule with a
  `condition_tree` joins 2-10 conditions with AND/OR and raises one incident
  per server naming the matched conditions; its holds count evaluation passes
  and it judges every sample (no rolling average).

* **Maintenance** (alerting plan 2.7, see
  [maintenance-windows.md](maintenance-windows.md)): a server in a maintenance
  window in force — its own server, server group or server tag window, or the
  device it is linked to in maintenance — is not judged by CPU, memory, swap,
  filesystem, disk, network, clock, service and process rules. Nothing is
  raised; an incident already open stays open, is neither resolved nor
  notified, and is judged again by the first pass after the window. Restart
  and shutdown events inside a window are marked `maintenance_suppressed` and
  never announced. Availability rules honour windows through their own
  signals.
* **Dependencies** (G-13, see [dependencies.md](dependencies.md)): while a
  device → server dependency's parent is down, degraded or unknown, a new
  incident of a server metric, service or process rule is recorded with
  `suppressed_by_dependency`, the parent's name and status, and
  `notified: false`. Nobody is paged. If the server still breaches once the
  parent is healthy, the trigger is sent then (like a quiet-hours one); if it
  clears first, it resolves without a recovery notice.
* **Cooldown** (plan 2.8): a host or APM rule's cooldown is now enforced like
  the SNMP evaluator's. A trigger within `cooldown` seconds of the rule's last
  *notified* trigger for the same server and dimension (APM: service) is
  recorded with `notified: false` and `cooldown_until`, and sent by the first
  pass after the cooldown if the condition still breaches — delayed, never
  dropped while it breaches. An episode that clears inside the cooldown is
  recorded in the Alert Center without a page and without a recovery. A
  trigger that never went out (quiet hours, dependency, cooldown) starts no
  cooldown. Host incidents store `notified_at` for this.
* **Depth metrics** (G-9/G-10, migrate-208, `host_depth_metrics`):

  | Metric | Reading | Dimension | Judged |
  |---|---|---|---|
  | `host_swap_pct` | swap used / total, % (servers without swap: no reading) | — | rolling average, auto clear 2 points |
  | `host_disk_latency_ms` | slower of average read and write time per I/O, ms | disk | rolling average; Linux agents only |
  | `host_nic_errors` | receive + transmit errors per second | NIC | rolling average |
  | `host_clock_skew_s` | agent clock vs appliance clock, s, measured at every upload | — | every sample for `min_duration` |
  | `host_fs_days_left` | days until full at the 7-day least-squares growth (a day of history needed; not filling = healthy) | mount | `< N` / `<= N`; clears at N + 1 day by default; `min_duration` is a hold |
  | `host_auto_service_stopped` | an automatic service stopped unexpectedly | service | `min_duration` is a hold |

  The Windows agent reports disk times in whole seconds (gopsutil), so its
  latency reads about 1000 times too low; latency rules skip Windows servers
  until the agent reports real per-I/O times. The agent's disk queue length is
  a single in-flight count per sample (0 on Windows), so it is not alertable.
  A Windows pending-reboot rule is not offered: the agent does not report it.
  Event-log counts per log and level (`host_event_log_summary`) are collected
  but have no rule yet.

  *Automatic service stopped*: the agent reports start mode `auto` for every
  automatic service and cannot tell delayed-start or trigger-start services
  apart, and an enabled systemd one-shot unit that finished is `stopped` too.
  Such services end cleanly, so a stop counts only when the last recorded exit
  code is not 0 and not 1077 ("never started since boot"), or systemd says
  `failed`. A deliberate, clean stop of an automatic service is not reported by
  this rule; name that service in a *Service stopped* rule. The rule's target
  lists service names to leave out (`gupdate*, edgeupdate*`; comma separated,
  wildcards, case-insensitive, name or display name). A service that is no
  longer automatic, no longer installed or now left out resolves its incident
  without a recovery notice; a service snapshot older than 15 minutes plus the
  agent's freshness is unknown.

Readings near the threshold keep one decimal, so 91.5% is never shown as "92%"
and 90.3% never as "90%". The threshold is shown with the same precision.

Replaying the field pattern (memory hovering between 89.8% and 91.5% every
minute for 48 hours) now produces one incident and one notification. The old
behaviour produced a new incident and email at almost every crossing.

The appliance's own health (data disk, ClickHouse, backups, updates, licence,
clock, failing notification channels, remote sensors) follows the same
lifecycle as the "appliance" rule family; see
[appliance-health-alerts.md](appliance-health-alerts.md). A "Notification
channel failing" rule whose only channel is the failing one cannot reach
anyone: give it a second channel or rely on its escalation policy.

## Rule editor and rules list (1.25.18)

The alert wizard and the **Alert Manager** list cover every rule family the
server accepts (`dashboard/src/components/forms/alertRuleCatalog.ts` mirrors the
validators in `server/app/api/v1/alert_rules.py`):

* **Families**: Device / SNMP (status, SNMP, interface quality, template gauge
  and state metrics, F5 certificates), Server, Service Check (status,
  certificate days, response time), Monitoring gap, Virtualisation, Device
  Tracker, NetPath, ZenPlus appliance, SNMP Trap, Syslog, APM & RUM, NetFlow.
  Each offers only the operators, limits, clear level and scope its validator
  takes. Retired keys (`session_count`, `jitter`, the five canonical state keys)
  are not offered for new rules but still open on the rules that use them.
* **Timers shown are timers honoured.** A field the evaluator ignores is hidden
  or explained: no breach hold on events (reboot, speed change, traps, syslog,
  Device Tracker events, NetPath), no clear hold on status rules (cooldown is
  shown for every family, server and APM rules included, since plan 2.8), the
  flapping hold is labelled *Window*,
  the APM hold *Averaging window*, and event alerts say they expire after the
  event alert expiry window.
* **Server depth metrics** (migrate-208) are in the Server family with their
  units: swap (%), disk latency (ms), network interface errors (errors/s),
  clock offset (s), filesystem full in (days, `<` / `<=` only) and automatic
  service stopped (no limit; *Services to leave out (patterns)* is its target).
* **New percent rules** (SNMP CPU, memory, interface utilisation and the server
  CPU, memory, swap, filesystem and disk I/O rules) start with a clear level 5 points
  on the healthy side of the limit and a 900 s clear hold when those fields are
  left empty. Existing rules are never changed; an operator may type any value.
* **Stored operators** (`gt`, `gte`, ...) open as their symbols.
* **Template state metrics** are picked as states ("Status is Down"); the rule
  stores at most four AND-combined conditions (`template_states.conditions_for`).
* **Placeholders** offered are the ones a save accepts for the rule's template
  kind (`GET /alert-rules/template-defaults` → `variables`, plus the family's
  `extras`), so `{error}` is offered only where it is filled.
* The list shows each rule's trigger in words with its unit, its timers and how
  it closes, its channels (or "No channel — notifies nobody" when no channel,
  no rule policy, no legacy tier and no default policy for its priority would
  page anyone), and when it last fired (`last_triggered_at`, the newest alert
  of the rule). A family filter narrows the list; the **Recommended** tab lists
  `GET /alert-rules/recommended` with Accept (enabled, channels, escalation
  policy).

## What each email says

Every alert email carries an incident reference (`INC-…`), its first-seen time,
how long it has been active, and a lifecycle label: **New**, **Reminder n of N**
(escalation repeats), **Escalated to level n**, or **Recovered** (with the
total duration). The subject keeps the severity first:
`[WARNING] [New] Memory high on srv-a [INC-3f2a9c1d]`.

* **Interface errors** state the interval and direction:
  "152 errors in the last 60 s (150 in, 2 out)". The rule value is unchanged (in
  plus out errors per poll interval), so existing thresholds keep their meaning.
  Counter wraps are corrected; a counter reset or reboot reads as unknown, never
  as a spike. An unchanged cumulative counter never triggers.
* **Ping / packet loss**: with no reply, latency is shown as *unavailable (no
  reply)*, never 0.0 ms. Latency rules never fire on an unreachable device. The
  banner shows reachability (e.g. DOWN) and the rule's severity separately.

## Message wording (1.25.3)

Every notification (mail, SMS, chat/webhook, escalation and the in-app alert
text) says what happened, to which asset, when, and what it means now. The
shared wording lives in `app.services.alert_phrasing`.

* **Asset**: type, name and IP — "Switch core-sw-01 (10.0.0.1)", "Server
  app-01 (10.0.0.5)". The type comes from the device type (unknown = "Device").
  Subjects keep the plain "name (IP)" so existing inbox rules still match.
* **When**: state changes and events carry the appliance-local time from the
  organisation's regional settings ("went down at 14:44"); an event on another
  day reads "Nov 07 14:44". SMS also ends with the full local timestamp.
* **Readings**: "Host memory on Server srv-a (10.0.0.5) rose to 97% (limit
  90%)", and on recovery "returned to 42% (limit 90%). Lasted 12 minutes."
  On/off states (ping, interface, reboot flags) never show a raw value such as
  "currently 1" or "now 0".
* **Events** (reboots, server restarts and shutdowns) say when and what now:
  "Switch sw-09 (192.168.24.34) rebooted at 14:44. Uptime is now 3 min (was
  41 days).", "… rebooted again at 14:52 (2 reboots since 14:44). Uptime is now
  1 min.", and on close "… has stayed up for 16 min since its 14:44 reboot; no
  further reboots". A server restart closes with "has stayed up since its 14:34
  restart and is reporting normally (up 13 min)", or, when its agent is silent,
  says that its current state is unknown. Nothing refers to an internal
  observation window. The built-in agent-offline alert states its last
  heartbeat in the regional time ("Agent on srv-01 is offline. Last heartbeat
  2026-11-19 14:44 GMT+3.").
* **Where** (1.25.18): every family with an asset (device status, SNMP, server,
  service checks through their linked device, traps, syslog, Device Tracker)
  offers `{site}` and `{group}`. The site is the one alert storms and warning
  grouping use: the server's site or the site of the sensor that polls the
  asset, else the device's location text; the group is the device's primary
  group (a server's or check's linked device). The mail's details table shows
  "Site" and "Group" when known, and the default SMS names the site after the
  rule only when known, capped at 24 characters (`{site_suffix}`):
  "ZenPlus CRITICAL - Device Down at Site Riyadh-HQ: Switch RUH-CORE-SW-01
  (10.120.30.40) went down at 14:44: it does not answer ping." NetPath, APM and
  appliance alerts have no site. The dispatcher (`notification_context`) fills
  any `{site}`, `{group}` or `{site_suffix}` a sender left, so none is sent as
  typed.
* **Chat and incident channels**: Slack, Teams, Discord and PagerDuty lead with
  the event sentence (never "srv-01 is ALERT"), then asset, site, severity and
  priority, value, the time in the regional settings, the duration on a
  recovery, the rule, the incident reference and a "View incident" link.
  Generic webhooks and PagerDuty `custom_details` get the documented field set
  of `docs/alerts-api-contract.md` ("Webhook payload"), never recipient
  addresses or the HTML mail.
* **Templates**: a rule's own templates may use only the placeholders its
  senders fill; an unknown `{placeholder}` is refused on save (422 with the
  unknown and the valid names). NetPath, Device Tracker and APM threshold rules
  send their own templates like every other family (they used to ignore them).
* Messages stay short enough for one or two SMS segments (no SMS length limit is
  enforced by the gateway code; the defaults stay near 160 characters, and a
  realistic device-down SMS with its site fits one GSM-7 segment).

### Reboot and restart alerts: auto-clear

A reboot (sysUpTime went back by more than 60 s; a 497-day TimeTicks wrap is not
a reboot) and a server restart/shutdown (agent OS lifecycle evidence) are
events, not levels. The rule's operator, threshold, reset level and breach hold
do not apply to them and are no longer offered in the editor; stored values are
kept as they are.

* One incident per rule + device/server. A further reboot or OS boot while it is
  open updates the same incident, says "rebooted again" / "restarted again"
  (one notification per further event, lifecycle *Changed*) and restarts the hold.
* **Auto-clear after** (`clear_duration`, empty = 15 minutes): the incident closes
  once that long passed without another event; operators can resolve it by hand
  at any time. A reboot or restart already announced is never paged again, also
  after a manual resolve.
* The optional closing notice says what is known now (stayed up / reporting
  normally, or no current reading), never "end of the observation window".
* Restart evidence older than 15 minutes when it first arrives (agent backlog)
  never pages.

### Interface quality: error rate, flapping, speed changes

A per-poll error count says little on its own: one access port raised 29
"Interface errors high" incidents reading "826 errors in the last 90 s" while
moving millions of packets. Four interface metrics (migrate-205) describe the
link instead. They use the usual interface selection (target name or index,
interface role), template scope, overrides, exclusions, maintenance and
dependency suppression, one incident per rule + device + interface.

* **Interface error rate / discard rate** (`if_error_rate_pct`,
  `if_discard_rate_pct`): errors (discards) as a percentage of the packets in the
  same poll interval, for each direction, the worse one being the reading.
  Packets are the unicast packet counters the poller already collects (64-bit
  `ifHC*UcastPkts` when both directions answer as Counter64, else the 32-bit
  pair; the width never mixes). Inbound errors and discards are not delivered,
  so they are added to the inbound packets; outbound packets already include
  them. Multicast and broadcast packets are not counted, so on a mostly
  broadcast port the rate reads somewhat high. A direction with fewer than
  1,000 packets in the interval is not judged (no reading, never a breach); a
  counter wrap is corrected and a reset reads as unknown, as for errors. The
  message names both directions: "Interface error rate 3% of packets (limit 1%
  of packets): inbound 3% (3000 errors in 97000 packets), outbound 0% (0 errors
  in 50000 packets) in the last 60 s". The per-poll counts `if_errors` /
  `if_discards` keep working unchanged.
* **Interface flapping** (`if_flaps`, `>` or `>=` a number of changes): the
  oper-status changes into or out of *up* within the rule's window. The window
  is the rule's hold (`min_duration`, empty = 15 minutes) and replaces the
  breach hold; an override's hold changes the window for its assets. A count is
  given only once polls cover the whole window. Changes are seen from the polls
  (`snmp_if_metrics.oper_status`), so a bounce shorter than one poll interval is
  missed: the count can be low, never high. Down ↔ lower-layer-down is not a
  change. The incident clears once a whole window holds no change (plus the
  rule's `clear_duration` when set); missing polls never clear it. Admin-down
  ports keep being counted so a port shut down mid-flap clears. "Interface
  Gi1/0/24 on Switch core-1 (10.0.0.1) changed state 6 times in 15 minutes; it
  is up now." / "… has not changed state for 15 minutes; it is up now."
* **Interface speed changed** (`if_speed_changed == 1`): the negotiated speed of
  an *up* interface changed. A `device_interfaces` trigger keeps the speed last
  seen while up and stamps `speed_changed_at` with the previous speed, so speeds
  reported while the link is down (often a default) never count and a link that
  returns at its usual speed is not a change. The first poll after the upgrade
  only sets the baseline. Like a reboot it auto-clears after `clear_duration`
  (empty = 15 minutes) with no further change; a further change keeps the same
  incident open. "Interface Gi1/0/24 on Switch core-1 (10.0.0.1) now runs at
  100 Mb/s (was 1 Gb/s)." Duplex is not collected, so there is no duplex rule.
  Wireless, cellular and LAG interfaces change speed by design: scope such rules
  by role or target.
* Flapping and speed-change conditions need a rule of their own (no other
  conditions). The recommended default rules offer *Interface error rate high*
  (> 1% for 10 minutes, warning P3) and *Interface flapping* (≥ 5 changes in 15
  minutes, warning P3).

## Each event is sent once

Both API workers run the evaluators. Each evaluator pass, and the escalation
sweeper, now takes a cross-worker lock and skips the tick if the other worker
holds it. A trigger in flight is marked `sending`, so no other pass announces it
again.

Every send for a known incident claims a **delivery key** before it goes out:
(incident, lifecycle event, channel, recipient), stored in `alert_delivery_keys`.

* **accepted** — the provider took it. Never re-sent.
* **failed** — the provider refused it. The next attempt may retry.
* **uncertain** — a timeout or dropped connection after the data was sent.
  It is never re-sent, so a recipient may miss one rather than receive two.
* **claimed** — in flight. A claim older than 10 minutes (the worker died
  mid-send) may be claimed again, so a crash cannot lose a page for good.

Channels and recipients are de-duplicated across the channels of one dispatch.
Duplicate channels are removed when a rule is saved. Exactly-once SMTP delivery
is not promised; every outcome is auditable in the delivery log.

## What was sent

The delivery log (`alert_deliveries`) records, for every channel and recipient
of every notification, the outcome: accepted, failed or skipped, with the error
and latency. Since migration 197 an accepted or failed row also keeps what the
message said, so a complaint about a wrong SMS or email can be checked later:

* **Email**: the subject and the plain-text body exactly as handed to SMTP
  (after localisation). The HTML part is the same content and is not stored.
* **SMS**: the text exactly as handed to the gateway, including the timestamp
  the dispatcher adds. One row per phone.
* **Slack / Teams / Discord / PagerDuty / webhook**: the title (the event
  sentence) and the facts shown with it, as "Label: value" lines (PagerDuty: its
  summary; a generic webhook: its `subject` and `message`). Never
  URLs, headers, routing keys or a channel's custom body template, which may
  embed credentials.
* **Recipients**: only the number (`recipients_count`). Addresses and phone
  numbers are not stored.

The text is capped at 8,000 characters. Skipped and held deliveries sent
nothing, so they store no text. Rows written before the migration show no
message.

Retention: subject and text are cleared after
`system_settings['alert_delivery_content'].days` (default 30, 1–365; `GET/PUT
/alerts/delivery-content`, changes are audited). The row itself (channel,
outcome, error, recipient count) stays. The sweep runs at most hourly from the
escalation sweeper loop.

On the incident page, *Routing & delivery → Delivery attempts* has a **Message
sent** view per attempt. Users with `alerts.view` see the subject and recipient
count; the text itself may contain names and addresses and is returned by the
API only to users with `alerts.manage`. For SMS the view shows the character
count, the encoding and the number of parts the phone receives: GSM-7 fits 160
characters in one message and 153 per part, while a single character outside
the GSM-7 alphabet (an em dash, Arabic, an emoji) switches the whole text to
UCS-2 with 70 per message and 67 per part.

## Event alerts: one row per condition and expiry

SNMP trap, syslog, Device Tracker (new, rogue, watched and moved endpoint) and
NCM configuration-change alerts record that something *happened*. No evaluator
ever sees them recover, so they used to stay open until someone closed them,
and every repeat added another open row (rogue-endpoint alerts open for 106
days, four of them for the same endpoint).

**One open row per condition** (`app.services.event_alerts`). A repeat of the
same condition while its row is open (active or acknowledged) updates that row
instead of inserting a new one:

| Source | Condition |
| --- | --- |
| SNMP trap | rule + device + trap OID (rule + source address + trap OID for an unknown device) |
| Syslog | rule + device (rule + source address for an unknown device) |
| Device Tracker event | rule + event kind + endpoint MAC (`udt_dedupe`) |

The row keeps the first event's message and trigger time and records the
history in metadata: `occurrences`, `last_seen`, `last_message` and the newest
ten occurrences (`recent_occurrences`, with event ids, so a retried trap
delivery is still recognised). The alert detail shows Occurrences, Last
occurrence and Last message once a row stands for more than one event.

Paging is unchanged: the rule's cooldown (floor 60 s for traps and Device
Tracker events) still decides whether a repeat pages. A repeat pages again once
the cooldown has passed since the condition's last page
(`metadata.last_notified_at`, else the trigger time), keyed with delivery event
`trigger:repeat:<occurrence>` — still a trigger for the storm policy and digest
grouping, as the new rows it replaces were. After the row is closed, the next event raises a new
incident. NCM backup failures already keep one incident per device
(`ncm_incidents`) and recover on the next good backup.

**Expiry** (`app.services.alert_expiry`, settings
`system_settings['alert_event_expiry']` = `{"enabled": true, "hours": 168}`,
on by default, 1–8760 hours). An open event alert whose last occurrence
(`last_seen`, else `triggered_at`) is older than the window is resolved with
`metadata.resolved_by = 'expired'`, closure reason `expired` ("Expired — no new
event within the window") and an `expired` lifecycle event. It sends no
notification, and the escalation sweeper sends no all-clear for it. Expired
rows are not recoveries: they stay out of time-to-recover figures and appear
under "other closures".

Expired: trap rows, syslog rows, Device Tracker event rows and NCM
`config.changed` rows. Never expired: Device Tracker port capacity (a level
with its own recovery), NCM backup failures, baseline anomalies, and every
metric, status, service or server incident.

The sweep runs from the escalation sweeper loop at most once a minute, in one
API worker at a time (`zenplus:alert_expiry_pass`), closing at most 500 rows per
pass. The policy is edited under Escalation & On-call → Alert storms ("Event
alert expiry"; `GET`/`PUT /alerts/event-expiry`, `alerts.view` /
`alerts.manage`, audited as `alert.event_expiry.update`).

## Scheduled reports

A schedule stores its hour and minute together with its own IANA time zone. An
08:00 Asia/Riyadh daily schedule runs at 05:00 UTC every day; the schedule list
and form show both ("08:00 Asia/Riyadh (05:00 UTC)"). Changing the organisation
time zone no longer moves existing schedules. On upgrade every schedule is
pinned to the zone it runs in today, so nothing moves by itself.

A schedule whose name contains a different time (for example "08:00" in the
name, 05:00 stored) is flagged. Correct the hour in the form. Manual runs are
labelled *Manual run*.

Report emails group headline figures by section, with the section's scope and
"as of" time (for example "Open alert backlog · all alert sources"). Findings
name an action instead of repeating methodology notes. Database engine names are
not shown in customer report text.

## Priorities and the escalation matrix (1.24.18)

Every incident carries a priority: **P1 Critical**, **P2 High**, **P3 Warning** or
**P4 Information**. A rule may set it. Otherwise it follows severity: critical is
P2, warning is P3 and info is P4. P1 is reserved for rules that say so, because
a critical CPU reading is not a major outage. Acknowledgement targets are set per
priority (*Escalation & On-call → Response targets*). The defaults are P1 5 min,
P2 15, P3 60 and P4 none. Older severity-keyed settings are still read.

An **escalation policy** is a reusable list of steps. Each step has a delay after
the first page and its targets: an on-call schedule, a person or a channel. A policy
can also set:

* a repeat interval and repeat count for the current step;
* an unresolved-review interval, which keeps sending updates after acknowledgement;
* an acknowledgement expiry, after which an old acknowledgement re-opens escalation;
* a fallback route.

Routing order: the rule's own policy, then the rule's own legacy escalation tiers
(unchanged), then the policy that is the default for the incident's priority. A
priority default, including one created by *Create recommended matrix*, never takes
over a rule that already escalates.

* Acknowledgement stops step escalation and assigns an owner. It does not stop the
  unresolved-review updates.
* Resolution cancels pending steps, and everyone who was paged gets the all-clear.
* A step that reaches nobody (an empty schedule, a user without contacts), or whose
  deliveries all failed, goes to the fallback route. The incident timeline records
  a *coverage gap* or *route failed* event.
* Recipients are de-duplicated across the targets of a step.
* *Create recommended matrix* builds the four policies proposed in the field review,
  from your schedules and channels. *Preview* (dry run) shows who each step would
  reach without sending anything. *Coverage* lists unrouted priorities, schedule
  gaps and people without contact methods.

**On-call schedules** rotate an ordered list of people every N hours from a handoff
time, in the schedule's own time zone, so a 09:00 handoff stays at 09:00 across
DST. Layers can be limited to days and hours, and the higher layer wins when two
are on duty. Time-boxed overrides win over every layer. People manage their own
contact methods (email, SMS) and time zone under *My profile*.

Every alert email has **View incident** and **Acknowledge** buttons. Acknowledge
opens the incident after sign-in and asks for confirmation. A link alone never
changes anything, because mail scanners follow links.

## Grouping and delivery budgets (1.24.18)

Grouping is off after the upgrade. *Escalation & On-call → Grouping* has *Enable
recommended*.

* When grouping is on, **P3/P4 warning and information triggers** wait a short
  *group wait* (default 60 s). Related ones are then sent as **one summary** per
  group and channel. Groups are formed by the chosen keys: appliance, site,
  device group, scope tag, event family. The summary lists every child incident,
  with its reference, asset, condition, value, first-seen time and link.
* P1/P2 or critical incidents, recoveries, escalations and reviews are **never held**.
* A **per-recipient budget** (default 30 non-critical messages an hour) sends any
  overflow into a digest instead of dropping it. P1/P2 traffic is not counted when
  *critical reserve* is on.
* Every held message records its reason and release condition on the incident
  timeline and in the digest list. *Release now* sends a digest at once.
* A recovery for an incident whose warning is still waiting is not mailed on its
  own: the summary lists it as already recovered. A digest whose incidents all
  recovered is cancelled, and the reason is recorded.

Every notification source now goes through one dispatcher: device status, service
checks, metrics, traps, syslog, APM, NetFlow, Device Tracker and NetPath. Grouping,
budgets, recipient de-duplication and delivery keys therefore apply to all of them.
