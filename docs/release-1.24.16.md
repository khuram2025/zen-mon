# ZenPlus 1.24.16 — Quieter, clearer notifications

Metric alerts now follow one incident per rule, asset and dimension.
- **Hold and hysteresis:** a breach must persist for the rule's hold time, and the incident clears only below a separate clear level. Percent rules default to 2 points under the threshold.
- **Stable recovery:** the incident resolves only after the cleared condition has held for 5 minutes by default.
- **Missing data:** missing or stale samples never resolve an incident.
- A server whose memory hovers just above 90% now raises one incident instead of an email at every crossing.
- Readings near the threshold keep a decimal (91.5%, not "92%").

Duplicate emails are prevented.
- Evaluators and the escalation sweeper no longer run the same pass in both API workers.
- Every notification claims a durable delivery key per incident event, channel and recipient before it is sent. Interrupted sends are retried; uncertain ones are not.

Emails now carry an incident reference, first-seen time, duration, and a New, Reminder, Escalated or Recovered label.
- Interface-error mails state the interval and direction, and counter wraps no longer create spikes.
- An unreachable device shows latency as unavailable instead of 0.0 ms.

Scheduled reports keep their own time zone. An 08:00 Asia/Riyadh daily report runs at 05:00 UTC, and the schedule pages show local time and UTC side by side. Existing schedules are pinned to the zone they use today; a name whose time differs from the stored time is flagged. Report emails label each figure's scope, findings name actions, and engine names no longer appear.

Migrations 162–163 are additive. If the organisation time zone was stored with stray spaces (which previously made schedules run in UTC), schedules now run in that zone. Server CPU, memory and disk rules keep their averaged trigger. No customer threshold changes. See `docs/alert-notification-policy.md`.
