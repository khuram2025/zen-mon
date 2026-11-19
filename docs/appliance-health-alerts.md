# Appliance health alerts

ZenPlus now raises alerts about its own health. Before this, a full data disk,
a stopped ClickHouse, a failed backup or update, an expiring licence, an
unsynchronised clock or a failing notification channel produced no alert. One
customer appliance failed 180 deliveries in 30 days and nobody knew.

Code: `server/app/services/appliance_alert_service.py`. Migration:
`scripts/migrate-198-appliance-health-alerts.sql`. Plan reference: alerting
audit 2026-11-19, gap G-1.

## The rule family

Appliance rules are ordinary alert rules (`alert_rules`) whose metric starts
with `appliance_`. The rule API classifies them as family `appliance`. They are
not tied to a device or a server, so the device scope fields (device, group,
type, location, tag, template, interface role) are cleared when a rule is saved.
Only unrestricted users can create or change them.

| Metric | Reading | Dimension | Source (read only) |
|---|---|---|---|
| `appliance_disk_pct` | Data volume used, % | none | Storage watchdog `state.json` while its heartbeat is fresh, else a direct `statvfs`. The data volume is the filesystem behind `/data` (often the root LV), else the one holding ClickHouse, else `/`. |
| `appliance_db_down` | 1 while ClickHouse does not answer `SELECT 1` within 15 s | `clickhouse` | ClickHouse client |
| `appliance_backup_failed` | 1 when backups are scheduled and the last scheduled backup failed, or none finished within the period plus 4 h (daily: 28 h, weekly: 172 h). The period counts from the last finished scheduled backup, or from when the schedule was saved. 0 when no schedule is set. | none | `storage_backups`, `system_settings['storage.backup_schedule']` |
| `appliance_update_failed` | 1 when the last finished update attempt failed or was rolled back; 0 after a success | none | updater `update-history.json` |
| `appliance_license_days` | Days until the licence expires (negative once expired) | none | Subscription synchronised from zentryc.com, else the local `subscriptions` row |
| `appliance_time_unsynced` | 1 while `timedatectl` reports NTPSynchronized=no | none | `timedatectl show` |
| `appliance_delivery_failures` | Failed deliveries in the last hour | channel (plus direct escalation recipients when they failed) | `alert_deliveries` |
| `appliance_sensor_offline` | 1 while the sensor sweep has the sensor offline; online or degraded is 0 | sensor | `sensors` |

An optional rule `target` limits a channel or sensor rule to one channel or
sensor, by id or name.


Security and operations events (plan gap G-16, `scripts/migrate-206-security-ops-alerts.sql`)
are three more metrics of the same family:

| Metric | Reading | Dimension | Source (read only) |
|---|---|---|---|
| `appliance_login_failures` | Failed sign-ins in the last 15 minutes | the account (`user:<name>`) when the name typed is an existing account (matched ignoring case); otherwise the source address (`ip:<address>`), which also covers single sign-on failures that carry no name | `audit_logs` rows `auth.login_failed` |
| `appliance_admin_changes` | Security-relevant administrative changes in the last 15 minutes (event-like, see below) | none | `audit_logs` (actions listed below) |
| `appliance_report_failed` | 1 when the latest finished run of an enabled report schedule failed or was `partial`; 0 after `success` or `skipped`; unknown before its first run | report schedule | `report_schedules`, `report_runs` |

## Lifecycle

These rules follow the metric incident rules in
[alert-notification-policy.md](alert-notification-policy.md):

- One incident per rule + dimension.
- **Breach hold** (`min_duration`): the condition must be true on every pass
  for that long before the incident opens.
- **Clear level**: an explicit `reset_threshold` on the condition, or for the
  data disk 2 points on the healthy side. A `> 85` rule clears at or below 83%.
- **Stable recovery** (`clear_duration`, NULL = 300 s, 0 = at once).
- A missing reading never resolves an incident and never counts as clear or
  breach time. This covers a source that fails, an unknown value, and a gap
  of more than 3 minutes between passes.
- A recovery notice is sent when the rule has `recovery_alert` on and the
  trigger was sent. The schedule window and cooldown hold a trigger back.
  It goes out late once they allow it.

The readings have no stored history, so the hold start times are kept in
`appliance_alert_state` (rule + dimension). Passes run every 60 s in one API
worker at a time (`alert_worker_lock`, `zenplus:appliance_alert_pass`). Every
send goes through `host_alert_service.dispatch_to_channels` with `alert_id` and
`delivery_event`. The priority is stamped from the rule (`alert_priority`).

If an incident is resolved by hand while the condition persists, a new incident
opens once the breach hold has passed again, counted from the next pass. The
same rule's cooldown then delays its page. To stop it, acknowledge the incident
or turn the rule off.

When a channel is deleted, or a sensor is removed or disabled, its open
incident is closed without a recovery notice
(`metadata.resolved_reason`).

## Default rules (migrate-198)

All are enabled, with no channels and no escalation policy of their own. The
priority default escalation policy routes them, as it does the other seeded
rules.

| Rule | Condition | Severity / priority | Hold |
|---|---|---|---|
| Appliance disk high | `appliance_disk_pct > 85` | warning / P3 | 600 s |
| Appliance disk critical | `appliance_disk_pct > 95` | critical / P2 | 600 s |
| Database down | `appliance_db_down > 0` | critical / P1 | 120 s |
| Backup failed | `appliance_backup_failed > 0` | warning / P3 | 0 |
| Update failed | `appliance_update_failed > 0` | warning / P3 | 0 |
| Licence expiring | `appliance_license_days < 30` | warning / P3 | 0 |
| Licence expiring soon | `appliance_license_days < 7` | critical / P2 | 0 |
| Clock not synchronised | `appliance_time_unsynced > 0` | warning / P3 | 900 s |
| Notification channel failing | `appliance_delivery_failures >= 3` | warning / P3 | 0 |
| Remote sensor offline | `appliance_sensor_offline > 0` | critical / P2 | 180 s |

## Security and operations events (migrate-206)

### Failed sign-ins

Every rejected sign-in is already audited as `auth.login_failed` (password
form, MFA step and single sign-on callback; the reason and the source
address are in its metadata). The evaluator counts them per 15 minutes:

- **Per account** when the typed name is an existing account. The account
  name is shown ("user jsmith").
- **Per source address** otherwise: names that are no account, and single
  sign-on failures without a name. The typed text is never shown, because it
  is often a password typed into the name field. The text says "unknown
  accounts from one address" and how many different names were tried.
- The address itself stays in the incident's dimension key
  (`metadata.dimension`), not in the text: alert text passes `scrub()` and
  never prints addresses. Find it in **Settings → Audit log**
  (`auth.login_failed`, around the alert time).
- The text names the last reason (wrong password, wrong MFA code, account
  disabled, single sign-on error, ...) and says when the account was locked.
- A dimension with an open incident is read every pass (0 once its failures
  left the window), so it clears after its clear hold; quiet dimensions are
  dropped. At most 200 dimensions (most failures first) are evaluated per
  pass, plus every one with an open incident.
- Not counted: attempts refused by the sign-in rate limit (more than 10 a
  minute per account or 30 per address) before they reach the password
  check. These are not audited.

### Administrative changes

These audit actions count (`ADMIN_ACTIONS`):

| Area | Actions |
|---|---|
| Users | `user.create`, `user.delete`, `user.password_reset`, `user.mfa_reset`; `user.update` only when it changes the role, the role source, the visibility scope (`scope_tags`) or enables/disables the account |
| Roles | `role.create`, `role.update`, `role.delete` |
| Sign-in | `settings.auth.update`, `settings.auth.toggle` (LDAP, RADIUS, single sign-on), `settings.auth.policy` (password, lockout, MFA policy) |
| Access and HTTPS | `security.access.update` (management access list), `security.tls.config`, `security.tls.cert_installed`, `security.tls.cert_removed` |
| Keys and tokens | `apm.ingest_key.create`, `apm.enrollment_token.create`, `sensor.enrollment_token.regenerate` |
| Alerting removed | `alert_rule.delete`, `alert_rule.toggle` only when it turns a rule off, `alert_rule.exclude`, `notification_channel.delete`, `escalation_policy.delete` |

ZenPlus has no personal API tokens; the keys and tokens above are the
credentials an administrator can create.

The metric is **event-like**: one incident per burst.

- The incident opens when the changes in the last 15 minutes reach the
  threshold (default 1).
- Further changes join the open incident; nothing is re-sent.
- It resolves once no change has happened for `clear_duration` (NULL or
  default 900 s), counted from the newest change, with a recovery notice.
- Changes an earlier incident already reported (up to its newest change,
  `metadata.facts.last_event_at`) never count again. A burst is paged once,
  also when someone resolves it by hand.
- The text lists up to three changes, newest first, with who made them
  ("role Guests deleted by admin; and 2 more").

### Report runs

- One incident per enabled report schedule.
- A run counts once it is finished: `running` runs and runs waiting for a
  delivery retry (`next_retry_at` set) are skipped, so a temporary SMTP
  failure that a retry fixes never alerts.
- Any run of the schedule counts (scheduled, "Run now" or retry).
- `skipped` (no data, the schedule says skip) is a good run.
- It clears on the next good run (`clear_duration` 0 in the seed).
- A schedule that is deleted or turned off closes its incident without a
  notice.
- After the upgrade, an enabled schedule whose latest run failed alerts once:
  it is still failing.

### Default rules (migrate-206)

| Rule | Condition | Severity / priority | Hold / clear | Enabled |
|---|---|---|---|---|
| Repeated failed sign-ins | `appliance_login_failures >= 10` | warning / P3 | 0 / 900 s | yes |
| Administrative changes | `appliance_admin_changes >= 1` | info / P4 (groupable) | 0 / 900 s | **no**: watching administrators is a policy decision |
| Scheduled report failed | `appliance_report_failed > 0` | warning / P3 | 0 / 0 | yes |

Examples:

- `ZenPlus appliance: 12 failed sign-ins for user admin in the last 15 minutes (limit 10; last reason: wrong MFA code); the account was locked`
- `ZenPlus appliance: 14 failed sign-ins for unknown accounts from one address (5 names tried) in the last 15 minutes (limit 10; last reason: wrong username or password)`
- `ZenPlus appliance: 2 administrative changes in the last 15 minutes: alert rule Device Down turned off by ops; user jsmith role set to admin by admin`
- `ZenPlus appliance: no further administrative changes for 15 minutes`
- `ZenPlus appliance: report schedule "Weekly SLA" failed: nothing was delivered (run at 08:00) (error: …)`
- `ZenPlus appliance: report schedule "Weekly SLA" was delivered again`

Limits:

- A password spray that stays below the limit on every account (few tries
  each, from many addresses) is not detected; use the Sign-in Activity
  report.
- Changes made straight in the database, or by actions not audited, are not
  seen.
- The rule wizard offers these metrics (see **Dashboard** below) and the rules
  list names them "Failed sign-ins", "Administrative changes", "Scheduled
  report failed". Not done yet: the alert detail could link to the Audit log
  filtered by `auth.login_failed` or by the listed actions (for users with
  `audit.view`), and a failed-report incident to the schedule's runs.

## What the alert says

The alert has no device or server. Its entity is "ZenPlus appliance", shown in
the alert list as a system entity (`metadata.source = 'appliance_health'`,
`metadata.entity_name`). A remote sensor incident also sets `sensor_id`, so it
links to the sensor. Examples:

- `ZenPlus appliance: data disk at 91% (limit 85%)`
- `ZenPlus appliance: ClickHouse database is not answering (timeout)`
- `ZenPlus appliance: the last scheduled backup failed (error: …)`
- `ZenPlus appliance: update to 1.25.18 failed and was rolled back (error: …)`
- `ZenPlus appliance: licence expires in 12 days, on 2026-12-01 (limit 30 days)`
- `ZenPlus appliance: clock is not synchronised (NTP service inactive)`
- `ZenPlus appliance: email channel NOC mail failed 5 deliveries in the last hour (last error: …)`
- `Remote sensor branch-01 is offline: checks it runs are not being made (last heartbeat 14:02)`

Error text is cleaned before it is shown. URLs, email addresses, IP addresses,
host names, phone-like numbers and `password=` / `token=` style values are
replaced, so an alert never prints credentials, addresses or tokens.

A rule's own message templates still apply. The built-in wording places the
sentence above in `{event_sentence}`.

## Limits

- **Failing channel.** A channel-failure alert goes to every channel of its
  rule and to its escalation route, like any other alert. If the rule's only
  channel is the one that is failing, the alert cannot reach anyone. Give the
  rule a second channel, or leave it on the default escalation policy.
- **PostgreSQL.** ZenPlus cannot alert that PostgreSQL is down. PostgreSQL
  holds the rules, the incidents and the channels, so no pass runs without it.
  `appliance_db_down` therefore watches ClickHouse. Watch PostgreSQL and the
  controller itself from outside with `GET /api/v1/system/health`, as for the
  controller-monitoring alert.
- **Exclusions and snoozes** are per device or server and do not apply to
  appliance rules. To silence one, turn the rule off or give it a schedule.
- **Remote sensors.** The sensor sweep no longer writes its rule-less
  `sensor_offline` alert row. That row had no channel, no escalation and paged
  nobody, and keeping it would duplicate the rule. migrate-198 closes rows of
  that kind that are still open (`resolved_by = 'migration'`). The sweep still
  marks the sensor offline and still suppresses its devices' and checks'
  alerts. The rule-less `sensor_degraded` warning is unchanged. If the
  "Remote sensor offline" rule is turned off, nothing alerts when a sensor
  goes offline.
- **Dashboard.** The rule wizard offers them under **ZenPlus appliance**: one
  condition, no device, server or tag scope, and for the channel and sensor
  rules an optional channel or sensor (`target`). Flag metrics (database,
  backup, update, clock, sensor, report) store their fixed `> 0`; the
  administrative changes rule shows *Auto-clear after* instead of a hold.
  The rules list shows the condition in words ("Data disk used > 85%").
- **Storage.** Nothing here changes storage. Disk and LVM changes still go only
  through the root volume helper, and pressure purging stays with the storage
  watchdog.
