# NCM change detection and re-encrypted secrets

A configuration change alert ("Running configuration changed") is raised when the
normalized text of a new backup differs from the previous one
(`app.services.ncm_content.normalize_config`). Normalization removes runtime noise
that changes without anyone changing the configuration.

## FortiOS re-encrypts secrets on every backup (1.25.3)

FortiOS prints every reversible secret as `set <name> ENC <ciphertext>` with a fresh
random salt each time the configuration is shown (IPsec pre-shared keys, local
user passwords, RADIUS/LDAP secrets, SNMP communities, encrypted private keys). Two
back-to-back backups of an unchanged FortiGate therefore differed on hundreds of
lines. In the field four FortiGates raised a change alert on every scheduled backup:
321-590 lines "added" and "removed", all of them `ENC` lines, with an empty redacted
diff ("Sensitive configuration changed").

Comparison now replaces those ciphertexts with a fixed marker. What is still
detected:

* any change outside the encrypted values, including adding or removing a secret
  setting (the `set <name>` line remains);
* FortiOS administrator password hashes (`config system admin` → `set password ENC
  SH2…`), which print identically every time;
* certificates and unencrypted keys; every other platform's configuration is
  unaffected.

A change to only the value of a re-encrypted secret cannot be seen in the text of a
FortiOS backup; FortiOS reports such edits in its own configuration-change log,
which NCM event-triggered backups listen to.

After the upgrade a one-time background pass (on the NCM one-minute timer,
`app.services.ncm_normalization`) recomputes the stored comparison hashes, so
baseline drift is correct again, and closes open change alerts whose two compared
backups are still archived and are identical under the new comparison. Those
closures are recorded as administrative with the reason on the incident timeline;
nothing is sent. Older alerts whose backups were already pruned cannot be verified
and are left for an operator to close.

## NCM alerts in the alert pipeline (migration 201)

NCM alerts have no alert rule. They are notified to the device's NCM channels
(`device_ncm.notify_channels`). `ncm_notification_outbox` is still the durable
queue: the queue row is written in the same transaction as the alert, so a crash
loses nothing. Each due row is now sent through
`host_alert_service.dispatch_to_channels` (`app.services.ncm_alerts.send`) with:

- the alert id and a delivery event: `trigger` for a new alert,
  `trigger:repeat:<n>` for another change on an open configuration-change alert,
  `recovery` when it closes. Delivery keys therefore stop a retried row from
  reaching a recipient twice. A row on which a recipient definitely failed is
  retried, and the retry skips the recipients who already received it.
- the priority derived from the alert's severity (warning -> P3, info -> P4), so
  warning triggers may be held in digests when grouping is on;
- the event family `configuration`, which can be selected in the alert storm
  policy ("Configuration alerts").

Each row is leased for 10 minutes before it is sent, because the dispatcher
commits. Rows queued before the upgrade carry no alert id; they are sent once,
without keys. Webhook channels now receive the alert payload every other source
sends; it still includes the NCM `event_id`, `event_type`, `device_id`,
`metadata` and `timestamp`. The `X-ZenPlus-Event-ID` header is no longer sent.
NCM alerts are not escalated, because escalation policies route rule alerts.

### Compliance violations

A compliance policy that fails on a device raises one alert per device and
policy ("Configuration of Switch sw-01 (10.0.0.2) fails 2 checks of compliance
policy Cisco IOS / IOS-XE hardening: No default SNMP communities (critical), VTY
lines do not accept Telnet (high)."). The check runs after every evaluation: each
captured running configuration and the daily pass.

- Severity is the worst failing check: critical -> critical; high or medium ->
  warning; low or info -> info. When checks start or stop failing, the open alert
  is updated in place and no second notification is sent.
- The alert resolves, with a recovery notice, once the policy has no failing
  check and at least one check passed or is excepted. Checks that cannot be
  assessed (`unknown`, for example no validated configuration) are missing data
  and never resolve it.
- A policy that no longer applies (disabled, platform changed) closes its alert
  without a notice.
- Violations that already failed when migration 201 ran are a baseline. Their
  alerts open without a notification, and they close without one, so an upgrade
  does not send a notice for old findings.

### Running configuration not saved

For devices whose captures include the startup configuration, the latest
validated running and startup captures are compared after every capture. The
pair is compared only if both were taken within 15 minutes of each other. The
comparison ignores capture headers such as "Building configuration",
"Current configuration : N bytes" and "Using N out of M bytes". If the
difference persists for 4 hours across captures, an alert opens: "Running
configuration of Switch sw-01 (10.0.0.2) has changes not saved to the startup
configuration (5 lines differ); a reload would lose them." It resolves with a
recovery notice once a capture pair matches again. A pair taken too far apart is
missing data.

State lives in `ncm_alert_state`.

## Large comparisons

Comparing two versions (**Compare with previous**, **Show drift**, **Compare with
another device**) and counting the lines of a detected change run in the background,
so a large comparison never delays other requests to the appliance. Large inputs use
a line matcher that anchors on lines occurring once on each side (names, addresses,
policy ids), so a multi-megabyte FortiGate configuration with thousands of changed
lines compares in about a second. On repetitive text without such anchors it can
show a section as replaced where a slower search would find a smaller edit; the
added/removed counts remain accurate for the diff it shows.

Each comparison has a 15-second budget and a 20,000-line display limit. A
comparison over either limit shows "The differences are too large to show here"
with buttons to download both versions (redacted copies) for comparison with a
file comparison tool. The added/removed counts are still shown when counting
finished within the budget. Change alerts are raised either way; their line counts
are left out only when counting did not finish.
