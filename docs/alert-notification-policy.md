# Alert notification policy: metric incidents, delivery and reports

This page describes how ZenPlus turns metric readings into incidents and
notifications, and how it guarantees that one incident event is sent once. It
answers a field review of 196 notifications over 48 hours (October 2026). In that
review, 121 memory warnings came from readings between 90.1% and 91.5% against a
90% rule. Interface-error emails arrived twice, seconds apart. Daily reports
titled 08:00 Riyadh ran at 05:00 Riyadh.

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

Readings near the threshold keep one decimal, so 91.5% is never shown as "92%"
and 90.3% never as "90%". The threshold is shown with the same precision.

Replaying the field pattern (memory hovering between 89.8% and 91.5% every
minute for 48 hours) now produces one incident and one notification. The old
behaviour produced a new incident and email at almost every crossing.

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

## Not in this release

* Grouping related warnings into digests, and an escalation matrix with
  on-call rotations (proposed in the field review), are follow-up work.
  Escalation levels and repeats configured on a rule keep working and are now
  labelled as reminders and escalations.
* Device status alerts (ping) already have confirmation and flap holds; see
  `docs/device-status-policy.md`.
