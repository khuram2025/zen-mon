# Alert storm policy (correlation)

*Escalation & On-call → Alert storms.* Off by default; an upgrade changes nothing
until an operator turns it on.

A site power cut, a failed core switch or a firmware push that reboots a whole
access layer raises the same event on dozens of assets within a minute or two.
Without this policy every channel receives one message per asset.

## Behaviour

* **Event types** — choose which kinds of event are combined: devices down
  (device down/unreachable, server agent offline), reboots and restarts (device
  uptime reset, server restart/shutdown), service checks, interface, latency and
  loss, hardware, routing/VPN/HA, server services and processes, SNMP traps, syslog.
* **Threshold and window** — a storm starts when MORE than *N* distinct assets
  raise a selected event type within *W* minutes. Each incident counts once.
  Up to *N* assets are notified individually as before (it is not a storm yet).
* **Scope** — count across all assets, or separately per site (location), device
  group, scope tag set or monitoring appliance.
* **During a storm** each channel and recipient that would have been notified
  gets ONE combined notification (about 30 s after the storm starts, so a burst
  lands in one message) listing every affected asset, then an update every
  *update interval* while new events arrive. Recoveries of assets whose alert was
  part of the summary are reported in the updates; with *fold escalations* on,
  their escalation steps and reminders are too.
* **Storm over** — after *W* minutes without a new event of that type in the
  scope, every route that heard about the storm gets a closing summary: how many
  recovered and which assets are still affected. Their later recoveries are
  notified individually.
* Incidents notified individually before the storm started keep their own
  recovery notifications, so a pager that received "down" also receives "up".
* Every channel type is covered: email, SMS, webhook, Slack, Teams, Discord,
  PagerDuty and direct pages to on-call people. PagerDuty receives one incident
  per storm (dedup key `zenplus:storm:<id>`), resolved by the closing summary
  when every asset recovered.
* Incidents themselves are unchanged: each asset keeps its own incident in the
  Alert Center, with a timeline entry saying its notification was combined.

The storm runs before warning grouping and budgets; a storm summary is never
grouped or budgeted itself. Delivery uses the same durable delivery keys as every
other notification, so a summary is sent once per channel and recipient.

## Implementation

`app.services.alert_storm` (gate inside `host_alert_service.dispatch_to_channels`,
release through `notification_grouping` digests with reason `storm`), API
`/alerts/storm-policy` and `/alerts/storms`, schema
`scripts/migrate-184-alert-storm-policy.sql`, settings
`system_settings['alert_storm']`. Tests: `server/tests/test_alert_storm.py`,
`dashboard/scripts/test-alert-storm-ui.mjs`.
