# Device status: confirmation, recovery limits and flap detection

Device status (Up, Degraded, Down) is a **confirmed** state. A single slow, lossy or
unanswered ping is recorded as evidence in the ping history, but it no longer changes
the status shown on `/devices`, writes a status-log row or evaluates alert rules.
This follows the soft/hard-state model used by Nagios, Icinga and Checkmk, the
"X of the last Y polls" and recovery-threshold model of SolarWinds, Zabbix and
Datadog, and Nagios-style flap detection.

## Why

Before this change a device became Degraded on one 3-packet ping above the latency
limit (or with one lost packet, which is already 33% loss) and returned to Up on the
next clean ping. On the development appliance this produced 11,636 status changes in
eight days; 88% were Up/Degraded pairs that lasted exactly one poll cycle. Each one wrote
the status log, evaluated alert rules and reset the Uptime column.

Replaying the same eight days through the new policy with the appliance's own limits
(500 ms, 5%): status changes fall from 1,455 to 310 per day (−79%), notifications from
11,638 to 2,124 (−82%), and every one of the 219 outages that lasted 10 minutes or more
is still notified.

## How a status is decided

Each poll cycle produces one **check** per device. A remote sensor retries its ICMP probe
inside the cycle; that still counts as one check.

| Status | Entered when | Left when |
|---|---|---|
| Down | `down_confirm_checks` consecutive checks get no reply (default 3; remote sensors use `sensor_down_confirm_checks`, default 1, because they already retry in-cycle) | `up_confirm_checks` consecutive checks reply (default 1). A recovering device always returns to **Up** first; Degraded must then be confirmed on its own. |
| Degraded | at least `degraded_confirm_checks` of the last `degraded_window_checks` checks were slow, lossy or unanswered (default 3 of 5), evaluated on a responding check | `degraded_clear_checks` consecutive checks are at or below the **recovery limits** (default 3), **or** a full window (5 checks) has no slow, lossy or unanswered check |
| Up | otherwise | — |

* Slow or lossy means latency above `degraded_rtt_ms` or packet loss above
  `degraded_loss_pct` (strictly above; equal values do not count).
* Recovery limits default to 80% of the Degraded limits (`degraded_rtt_clear_ms`,
  `degraded_loss_clear_pct`). Between the two limits a device keeps its status
  (hysteresis), so it cannot bounce at the threshold.
* A failing check never enters Degraded: Down outranks Degraded.
* Maintenance windows keep the existing behaviour: status shows Maintenance, checks keep
  being evaluated, and the confirmed status is applied (with alerts) when the window ends.

## Flapping

A device is **flapping** when its confirmed status keeps changing. Flapping is a flag
shown next to the status, never a status of its own. It is tracked separately for:

* **reachability** — changes into or out of Down;
* **latency/loss** — changes between Up and Degraded.

So latency jitter can never hold back a Down notification.

Flapping starts when a class records `flap_start_changes` changes (default 4) within
`flap_window_minutes` (default 30). It ends when at most `flap_stop_changes` changes
(default 1) remain in the window, or when the status has not changed for
`flap_stable_minutes` (default 10).

Nagios counts state changes over the last 21 checks. ZenPlus ping intervals range from 10
seconds to an hour, so a fixed number of checks would cover 3.5 minutes on one device and
21 hours on another. ZenPlus therefore counts confirmed changes in a time window. A device
polled only every few minutes may never reach the start threshold; the device explanation
and the settings page show the longest interval at which flapping can still be detected.

### Notifications while flapping

* Status alerts are **held, not dropped**. While the transition's class is flapping, a
  status alert waits at least `flap_stable_minutes` (or the rule's own *Condition must
  exist for*, whichever is longer). It is sent only if the device is still in that status;
  otherwise it closes as a *hold cancelled* notice (never an incident, no recovery row). A
  sustained outage is therefore always notified. The Alert Center marks held rows
  *Held: device flapping*.
* Escalation levels and the acknowledgement target count from the moment a held alert was
  actually sent (`metadata.notified_at`), not from the original change.
* Recoveries follow their trigger as before. A trigger that was sent before flapping began
  still gets its recovery notice; a trigger that was held and cancelled recovers silently.
  Rules that trigger only on *Up* announce recoveries and are never held.
* One **Flapping started** and one **Flapping stopped** notice per episode, delivered in
  episode order from the stored episode (so a restart cannot lose them). Recipients are the
  in-scope, enabled device rules whose trigger matches the episode class (Down or Any for
  up/down flapping, Degraded or Any for latency/loss flapping), that are not snoozed and are
  inside their schedule. Each channel receives one message; PagerDuty-type channels never
  receive them, so they never open incidents. The stop notice goes only to the channels that
  received the start notice, as an all-clear when the device is Up. An episode that is
  already over when its start notice comes due sends neither notice. Notices are skipped
  while the device is in maintenance or its sensor is offline, are recorded as *info*
  notices without a rule (they never start a rule's cooldown) and are not counted in the
  device alert counts.
* A rule with **Notify immediately even while the device is flapping** bypasses the hold
  (for example a core-router Down rule).
* Device rules that use latency or packet-loss conditions are still evaluated only when the
  confirmed status changes.

## Settings

*Settings → General → Monitoring.* All values are global (per-device and per-group
overrides are not part of this release). A stored setting that is out of range is clamped
rather than rejected by the poller. Saving merges with the stored document, so an older
browser tab cannot reset the newer fields.

| Key | Default | Range |
|---|---|---|
| `degraded_rtt_ms` / `degraded_loss_pct` | 100 ms / 10% (existing values are kept) | (0, 10000] / [1, 100] |
| `degraded_rtt_clear_ms` / `degraded_loss_clear_pct` | 80% of the Degraded limits | up to the Degraded limit |
| `degraded_confirm_checks` / `degraded_window_checks` | 3 / 5 | 1–20, window ≥ confirm |
| `degraded_clear_checks` | 3 | 1–20 |
| `down_confirm_checks` / `sensor_down_confirm_checks` | 3 / 1 | 1–10 |
| `up_confirm_checks` | 1 | 1–10 |
| `flap_detection_enabled` | on | |
| `flap_window_minutes` | 30 | 5–1440 |
| `flap_start_changes` / `flap_stop_changes` | 4 / 1 | 3–64 / below start |
| `flap_stable_minutes` | 10 | 1 – window |

The **Legacy single-check** preset (`degraded_confirm_checks=1`, `degraded_window_checks=1`,
`degraded_clear_checks=1`, recovery limits equal to the Degraded limits, flap detection
off) closely approximates the previous behaviour without a downgrade. Remaining
differences: reason wording, transitions stamped with the sample time, counters that
survive a poller restart, and the Uptime origin described below.

## Where it shows

* **Devices list:** a Flapping marker in the Status cell, a Flapping filter
  (`?flapping=1`, applied by the API) that combines with the status filter, and a fleet
  count. The CSV export layout is unchanged. Maps are not part of this release.
* **Settings page:** the longest interval at which flapping can start is shown separately
  for up/down (central and remote sensor) and for latency/loss changes.
* **Device detail:** the explanation states the active policy, progress towards a pending
  change (for example *2 of 3 failed checks*), the flapping episode and when it can end at
  the earliest. The activity log groups the changes made during an episode.
* **Uptime** counts from the last time the device became reachable. Moving between Up and
  Degraded no longer resets it.
* **Availability** percentages remain sample based (`uptime_pct`, `sample_count`) and are
  unchanged.
* **Reports:** the *Unstable devices* section adds flap episodes and time spent flapping.
  Its change-count KPI is now labelled *Unstable nodes (≥ N state changes)*.
* Flap episodes are stored in `device_flap_episodes`; `device_status_log` keeps confirmed
  transitions only.

## Upgrade notes

* State is seeded silently from the current status. No status-log rows or notifications
  are produced by the upgrade itself. A device shown as Degraded at upgrade time stays
  Degraded until it meets the recovery rule, then records one Degraded → Up change that
  resolves its open alert.
* Degraded alerts arrive after confirmation (about 1–2 minutes at a 30-second
  interval), plus any *Condition must exist for* time on the rule. Rules that used a long
  hold to absorb single-sample blips can usually shorten it.
* Status-change counts in reports drop from the upgrade date onwards.

## For developers

* The model is one pure state machine with two implementations: Go
  `poller/internal/devicestatus` (central poller) and Python
  `server/app/services/device_status_machine.py` (remote-sensor ingest, explanation API,
  settings). Both replay `scripts/dev/device-status-contract/vectors.json`. Change the
  model in both, add cases to `build_vectors.py`, regenerate with
  `python scripts/dev/device-status-contract/build_vectors.py` and run both test suites.
  Reason strings are part of the contract.
* Persisted state is in `device_status_state` (one row per device and owner); the devices
  row mirrors `is_flapping`, `flapping_since`, `flap_kind` and `status_changed_at`.
* Only the polling owner of a device runs the machine. Another vantage's samples go to
  ClickHouse only.
