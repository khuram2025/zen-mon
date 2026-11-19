# Certificate-expiry and response-time alerts

Before this change, a certificate could only raise an alert through a TLS service
check. The check turned *warning* or *down* at its fixed `tls_warn_days` /
`tls_critical_days` (30 / 7 days), and that status change was the alert. Nothing
could alert on a service check's response time. The F5 BIG-IP certificate
inventory could not be alerted on at all. Three rule metrics now cover these
cases. They are evaluated by `app.services.service_metric_alert_service` every
60 seconds (migration 199).

| Metric | Reading | Asset (dimension) | Scope |
|---|---|---|---|
| `service_tls_days` | Whole days until the certificate the check last saw expires (`service_checks.tls_expiry_date`, else `tls_days_remaining`) | service check | service check, service check group, linked device, tag |
| `service_response_ms` | Average response time (ms) of the check's **passing** runs over `max(min_duration, 5 min)` | service check | service check, service check group, linked device, tag |
| `f5_cert_days` | Whole days until each certificate in an F5 BIG-IP's certificate inventory expires | device (certificate) | device, device group, type, location, tag |

The rule wizard offers `service_tls_days` and `service_response_ms` under
**Service Check** (Watch: certificate days left / response time; scope: one
check, a check group, the checks linked to a device, or a tag) and
`f5_cert_days` under **Device / SNMP** (device scopes). Each rule takes one condition
(`<`, `<=`, `>` or `>=`). An optional `reset_threshold` sets the clear level and
must lie on the healthy side of the limit. Rules for these metrics are never
fired by service status changes.

## Readings

- **Certificate days**: read only from a fresh run, one within three check
  intervals and at least within 15 minutes. A failed run stores no certificate,
  so it gives no reading. An expired certificate reads as a negative number of
  days.
- **Response time**: read from ClickHouse `service_metrics` with `is_up = 1`,
  from every vantage that runs the check. Failed runs never count: a failing
  check is handled by the status rules. A check that runs less often than the
  averaging window uses its latest passing run if that run is within two check
  intervals. The message then says "latest run". If ClickHouse cannot be read,
  the pass changes nothing.
- **F5 certificates**: read from `device_template_values`
  (`f5_cert_expiry_epoch`). The reading is missing when it is more than 3 h 15 min
  old, because the inventory polls hourly. The inventory lists every installed
  certificate, including unused and default ones. It does not show whether a
  certificate is used by live traffic. Use a TLS service check for that.

## Lifecycle

The lifecycle matches the other metric incidents
([alert-notification-policy.md](alert-notification-policy.md)):

1. There is one incident per rule + asset + dimension. The asset is a service
   check or a device; the dimension is the F5 certificate.
2. The condition must hold for *Condition must exist for* (`min_duration`)
   before the incident opens and the trigger is sent.
3. The incident clears at the explicit `reset_threshold`. Without one, it clears
   **1 day** on the healthy side for the day metrics: a `< 30 days` rule clears
   once the certificate has 31 days or more. For response time it clears
   **10 %** on the healthy side: a `> 1500 ms` rule clears at 1350 ms or less.
4. The cleared condition must hold for *Clear after* (`clear_duration`, NULL =
   300 s, 0 = at once) before the incident resolves.
5. A missing reading never resolves an incident and never counts toward either
   hold. A hold in progress starts again when data returns.

The breach and clear start times are stored in `service_metric_alert_state`, so
a restart does not reset a hold. Each pass takes the `alert_worker_lock`
(`zenplus:service_metric_alert_pass`), so only one API worker runs it. Every
send goes through `dispatch_to_channels` with the alert id and the delivery
event (`trigger` / `recovery`). Cooldown, notification schedules, late sends
after quiet hours, priorities, escalation and P3/P4 grouping work as they do for
other rules. A recovery is sent only if the trigger was sent and the rule has
`recovery_alert` set.

The evaluator skips these assets, and leaves any open incident on them as it is:

- service checks inside an active service maintenance window;
- devices in maintenance (manual or a device maintenance window);
- devices for which the rule is turned off with an `alert_rule_exclusions` row.

A certificate removed from an F5 stops producing readings, so its open incident
stays open until an operator resolves it.

## Wording

Messages use `app.services.service_metric_phrasing`:

- "Certificate for shop.example.com (Service check Shop HTTPS) expires in 12 days (on 2026-12-01; limit 30 days)."
- "Certificate /Common/shop.crt on Load balancer f5-01 (10.0.0.9) expired 2 days ago (on 2026-11-17; limit 7 days)."
- "Response time of Service check Shop HTTPS is 2.4 s (limit 1.5 s, 5-minute average)."

Recovery messages:

- "Certificate for shop.example.com (Service check Shop HTTPS) renewed: now expires in 364 days (on 2027-11-18)." The message says "renewed" only when the expiry date changed. Otherwise it says the certificate "is back within the limit".
- "Response time of Service check Shop HTTPS is back to 800 ms (limit 1.5 s, 5-minute average)."

Custom templates on a rule are rendered with `{event_sentence}`, `{asset}`,
`{certificate}`, `{expires_on}`, `{reading}`, `{threshold}`, `{check_name}`,
`{target}` and the usual `{rule_name}`, `{severity}`, `{duration}` and incident
placeholders.

## Recommended defaults (not seeded)

No rules are created automatically. These are the recommended starting rules:

- Certificate expiry warning: `service_tls_days < 30`, severity warning, **P3**.
- Certificate expiry critical: `service_tls_days < 7`, severity critical, **P2**.
- The same pair for `f5_cert_days` on F5 devices, where inventory alerts are wanted.
- Response time: no default. Each operator sets the limit for each check.

The TLS check's own warning/down status at `tls_warn_days` / `tls_critical_days`
is unchanged.
