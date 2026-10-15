# Device status contract vectors

`vectors.json` holds the device status machine (confirmed status, hysteresis, flap detection) to one behaviour in both
languages: the Python reference `server/app/services/device_status_machine.py` (remote-sensor writer, APIs) and the Go
package `poller/internal/devicestatus` (central poller). Both test suites replay every case and must reproduce every
expected state field, event and reason string exactly.

The file is **generated**: never edit it by hand. The case inputs live in `build_vectors.py`; the expectations come from
the Python reference.

```
python scripts/dev/device-status-contract/build_vectors.py          # rewrite vectors.json
python scripts/dev/device-status-contract/build_vectors.py --check  # exit 1 (with a diff) when vectors.json is stale
```

Run from the repository root with the server's Python requirements. No database, network or environment is needed and
the output is deterministic. `server/tests/test_device_status_machine.py` runs `--check`, so a change to the machine
that is not rebuilt into the vectors fails CI. When a behaviour change is intended, rebuild, review the diff of the
affected cases, and update the Go implementation in the same change.

## Format (`"version": 1`)

* `policy_cases[]`: `{name, raw, expect_effective}`. `PolicyFromSettings(raw)` must equal `expect_effective` (all 15
  keys; recovery limits are concrete numbers). `raw` may be `null` or a non-object.
* `reseed_cases[]`: `{name, row, owner, devices_status, status_changed_at, expect_reason, expect_state}`. The reseed rule
  on `row` (a state object or `null` for a missing row) must give `expect_reason` (`"owner_change"`, `"reseed"` or
  `null`); when it is not `null`, seeding from `devices_status`/`status_changed_at`/`owner` must give `expect_state`.
* `cases[]`: `{name, policy_raw, initial, seed?, checks[], expect[]}`.
  * `initial` is a state object, or `null`: then seed from `seed = {devices_status, status_changed_at, owner}`.
  * Replay: `policy = PolicyFromSettings(policy_raw)`; for each check, if it carries `policy_raw`, reload the policy from
    it (it applies from that check on); then `state, events = Step(state, check, policy)` and compare with the matching
    `expect[]` entry.
  * check: `{ts, responding, rtt_ms, loss, interval_s, maintenance, ordered, source, policy_raw?}`; `rtt_ms` is `null`
    when not responding (use 0), `loss` is a ratio 0..1, `interval_s` is whole seconds (`devices.ping_interval`),
    `source` is `central` or `sensor`, `ordered` is false for sensor samples.
  * expect: `{hard_status, displayed, in_maintenance, status_since, fail_streak, ok_streak, clear_streak, window_bits,
    window_len, reach_changes, perf_changes, reach_flapping, perf_flapping, flapping_since, last_sample_at, pending,
    events}`. `pending` is `Pending(state, policy, check.source)` after the check: `null` or
    `{target, count, required}`.
* state object: the `device_status_state` columns `{hard_status, in_maintenance, status_since, fail_streak, ok_streak,
  clear_streak, window_bits, window_len, reach_changes, perf_changes, reach_flapping, perf_flapping, flapping_since,
  last_sample_at, owner}`.
* event: always all eight keys `{type, old, new, reason, alert, flapping, kind, end_reason}`.
  * `transition`: `kind`/`end_reason` null; `flapping` = the transition's class (reachability or performance) is
    flapping after this check, false for maintenance entry/exit and changes from/to unknown.
  * `flap_start`: `old`/`new`/`reason`/`end_reason` null, `alert` false, `flapping` true, `kind` =
    `reachability|performance|both`.
  * `flap_stop`: `old`/`new`/`reason`/`kind` null, `alert` false, `flapping` false, `end_reason` =
    `stable|disabled|maintenance`.

Timestamps are RFC 3339 in UTC with `Z`, fractional seconds only when non-zero, without trailing zeros (this is Go's
`time.RFC3339Nano` output for UTC times; Python `format_ts`). Parse them as instants and compare instants. Numbers are
float64 in both languages; never decode `loss` into float32 (the float32 boundary values are already widened).

## Behaviour the vectors pin where DESIGN.md is silent

* Loader: `degraded_rtt_ms <= 0` takes the default (0 has always meant "unset"); `degraded_rtt_clear_ms <= 0` takes the
  derived value; `degraded_loss_pct` below 1 clamps to 1; numbers outside the float64 range take the default. `round3`
  is the correctly rounded decimal of the exact binary value, ties to even (Python `round(x, 3)`, Go
  `ParseFloat(FormatFloat(x, 'f', 3, 64))`), the same rounding `fmt3` prints. Never scale first:
  `math.Round(x*1000)/1000` turns 0.8 x 1.005625 (0.80449...) into 0.805 and the tie 1.0625 into 1.063.
* Flap step per class: the start test runs before the stop test on the same check, so a class whose history is already
  stable when it would start (re-enabled detection, lowered start count) starts and stops at once: no event, history
  cleared. A stop clears the history whenever the stability condition (last change at or before
  `ts - flap_stable_minutes`) holds, also when the count condition holds on the same check; a stop by count alone
  keeps it. Either way the end reason is `stable`.
* A transition's own change is recorded before the flap step; a flap start or stop on the same check sets that
  transition's `flapping`.
* Streaks follow DESIGN literally: the streak that caused a change keeps its value afterwards (e.g. `ok_streak` stays 1
  in `up` until a failure), and the window path from degraded resets `clear_streak`.
* An ignored sensor sample returns the state unchanged (including `last_sample_at`) and no events.
* A stored `hard_status` outside `up|degraded|down|unknown` (the Go zero value `""` included) is read as `unknown`.
  A change from or to `unknown` has no class: it is never recorded and its transition always has `flapping` false.
* Maintenance ending while the device is still down but answering (`up_confirm_checks` > 1, recovery not confirmed)
  reads `Maintenance ended; responding for {ok_streak} of {up_confirm_checks} checks needed to confirm recovery`; with
  a failure streak it reads `Maintenance ended; no response for {fail_streak} consecutive checks`.
* `pending`: a hard status of down reports `{up, ok_streak, up_confirm_checks}` while `ok_streak` > 0; any other status
  with `fail_streak` > 0 reports `{down, fail_streak, down confirmation of the source}` (DOWN outranks DEGRADED); up with
  bad checks in the window reports `{degraded, bad_count, degraded_confirm_checks}`; degraded with `clear_streak` > 0
  reports `{up, clear_streak, degraded_clear_checks}`; otherwise `null`. Progress towards the clean-window recovery is
  not reported.
