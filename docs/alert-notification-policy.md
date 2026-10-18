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
