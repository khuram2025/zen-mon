# Monitoring gap alerts

Alerting audit 2026-11-19, gap G-2. Before these rules, monitoring that
stopped delivering looked healthy: a switch whose SNMP community was changed
kept answering ping and showed *Up* with frozen graphs, and an agent whose
Server module had stopped kept heartbeating and showed *Online* with no new
metrics. Two rule metrics now page for that, evaluated by
`server/app/services/monitoring_gap_alert_service.py`.

## Metrics

| Metric | Asset | Reading | No reading (nothing raises or resolves) |
|---|---|---|---|
| `snmp_unreachable` | device | Minutes the controller poller's SNMP polls have gone unanswered (`device_snmp_poll_state.failing_since`); 0 while polls are answered | Device not up/degraded (Device Down's business); never polled; poll state older than `max(3 x snmp_poll_interval, 10 min)` (e.g. moved to a sensor); controller-managed children (never polled) |
| `agent_no_metrics` | agent server | Minutes since the last host metric batch (`agents.last_metric_at`); 0 while a batch arrived within 3 minutes | Heartbeat silent for `DOWN_AFTER_S` (180 s; the availability rules' business); the agent never delivered host metrics (APM-only installs) |

A rule takes one condition `> N` or `>= N`, N in minutes (1 to 10080), trigger
`any`. Scope: `snmp_unreachable` by device, group (with subgroups), device
type, location or tag; `agent_no_metrics` by group or tag (all agent servers
otherwise). A shared rule is turned off for one asset with an
`alert_rule_exclusions` row. Rules are created through `/api/v1/alert-rules`;
the rule wizard offers both under **Monitoring gap** (minutes, `>` or `>=`, no
clear level).

## Lifecycle

The metric-alert rules of AGENTS.md:

* one incident per rule + asset (`metadata.gap_key` = `gap:device:<id>` /
  `gap:server:<id>`);
* the gap must reach N, then hold for the rule's `min_duration`;
* an open incident resolves only once the gap has **closed** (reading 0) and
  stayed closed for `clear_duration` (NULL = 300 s). A gap that is open but
  below N neither raises nor clears;
* a missing reading never raises, resolves or counts toward a hold (the
  incident is marked `data_state: missing`);
* holds live in `service_metric_alert_state` (migrate-199), so they survive
  restarts and are shared by both API workers;
* every pass runs under `alert_worker_lock` (`zenplus:monitoring_gap_alert_pass`),
  every 60 s;
* sends go only through `host_alert_service.dispatch_to_channels` with
  `alert_id` and `delivery_event`; a recovery is sent only when the trigger
  was and the rule has `recovery_alert`; cooldown and schedule windows apply,
  and a trigger held by quiet hours is sent once the window opens while the
  gap is still open.

Devices in maintenance (manual status or a maintenance window covering the
device, its groups, a tag or everything; one-off or recurring) and servers in
a maintenance window or whose linked ping device is in maintenance are skipped
(`docs/maintenance-windows.md`); an open incident stays as it is.

## Wording

* "SNMP polling of Switch core-1 (10.0.0.1) has failed for 15 minutes while the
  device answers ping: check the SNMP credentials or ACL." The notification
  details carry the last SNMP error.
* "SNMP polling of Switch core-1 (10.0.0.1) is answered again."
* "The agent on Server web-01 (10.0.0.5) has sent no metrics for 20 minutes
  although it still sends heartbeats: check the agent's Server module and its log."
* "The agent on Server web-01 (10.0.0.5) is sending metrics again."

Assets are named as `alert_phrasing.asset_label` does. `event_family` is
`monitoring_gap`.

## How SNMP polls are recorded

After every SNMP poll the controller poller (`poller/internal/pinger`,
`snmp_poll_outcome.go`) writes one row per device in
`device_snmp_poll_state` (migrate-202; `poller/internal/store/postgres_snmp_poll.go`):

* **answered** when the device returned anything (system info, interfaces,
  any scalar, entities, sensors, template values, UDT data) or the poll ended
  without an error. A poll the per-device budget cut short after a reply is
  answered: the device speaks SNMP, it is only slow.
* **failed** otherwise; `failing_since` is the first failed poll of the
  current run, `last_error` the newest error (500 characters),
  `consecutive_failures` the run length. The first answered poll clears
  `failing_since`.
* nothing is recorded when the poller itself is stopping; an older poll never
  overwrites a newer one; a device deleted mid-poll is ignored; before
  migration 202 the poller warns once and retries every 10 minutes.

Devices polled only by a remote sensor have no row and so no reading.
