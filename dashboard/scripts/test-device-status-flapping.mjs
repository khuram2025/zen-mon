// Confirmed device status, recovery limits and flapping in the dashboard (docs/device-status-policy.md):
//   (a) components/devices/deviceStatus.ts, the pure module behind every page: the policy loader replayed against the
//       shared contract vectors (scripts/dev/device-status-contract/vectors.json), 3-decimal rounding, pending progress,
//       policy sentences, timing estimates, the flap-detection interval limit, the settings form <-> full document,
//       client validation, the alert flap labels and the activity-log grouping of a flap episode,
//   (b) the sentences the review found false are gone from the generated copy and from the sources,
//   (c) server-rendered pages: the Devices list (Flapping in the Status cell beside "SNMP: no data", the Flapping KPI
//       and its flapping=1 filter combined with Status), the status explanation, the Monitoring card and the alert chips.
// Run from dashboard/: node scripts/test-device-status-flapping.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { build } from 'esbuild'
import { transformWithEsbuild } from 'vite'

process.env.TZ = 'UTC'
const root = process.cwd()

const src = fs.readFileSync('src/components/devices/deviceStatus.ts', 'utf8')
const compiled = await transformWithEsbuild(src, 'deviceStatus.ts', { loader: 'ts', format: 'esm' })
const S = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)
const vectors = JSON.parse(fs.readFileSync(path.join(root, '..', 'scripts/dev/device-status-contract/vectors.json'), 'utf8'))
const utc = (ms) => new Date(ms).toISOString().slice(11, 16)
const T0 = Date.parse('2026-10-14T12:00:00Z')
const iso = (minutes) => new Date(T0 + minutes * 60_000).toISOString()

/* ═══ (a) the pure module ═══════════════════════════════════════════════════════════════════ */

// Rounding: the correctly rounded decimal of the exact binary value, ties to even (Python round(x, 3)), never x*1000.
{
  assert.equal(S.round3(0.8 * 1.005625), 0.804, '0.80449... rounds down; scaling first gives 0.805')
  assert.equal(S.round3(1.0625), 1.062, 'an exact tie goes to even')
  assert.equal(S.round3(1.0645), 1.065, 'not a tie in binary (1.06450000000000000177...): the exact value decides')
  assert.equal(S.round3(1.0635), 1.063, '1.06349999999999988... rounds down, as Python round(1.0635, 3)')
  assert.equal(S.round3(0.8 * 7), 5.6)
  assert.equal(S.round3(369.4935), 369.493)
  assert.equal(S.fmt3(5.6000000000000005), '5.6')
  assert.equal(S.fmt3(500), '500')
  assert.equal(S.fmt3(1234.5678), '1234.568')
  assert.equal(S.fmt3(0.0004), '0')
}

// The loader is the server's: every policy case of the shared contract vectors gives the same effective policy.
{
  assert(vectors.policy_cases.length >= 18, 'the vectors carry the loader cases')
  for (const c of vectors.policy_cases) assert.deepEqual(S.effectivePolicy(c.raw), c.expect_effective, c.name)
  assert.deepEqual([...S.POLICY_KEYS].sort(), Object.keys(vectors.policy_cases[0].expect_effective).sort(), 'the document has exactly the contract keys')
}

const DEF = S.effectivePolicy({})
const LEGACY = S.effectivePolicy(S.settingsPayload(S.legacyPresetForm(S.defaultsForm())))

// Pending progress: every pending state the vectors produce has words, with the counts.
{
  const seen = new Map()
  for (const c of vectors.cases) for (const e of c.expect) if (e.pending) seen.set(JSON.stringify([e.pending, e.hard_status]), [e.pending, e.hard_status])
  assert(seen.size >= 4, 'the vectors cover pending down, degraded and both recoveries')
  for (const [p, hard] of seen.values()) {
    const text = S.pendingText(p, { hardStatus: hard, windowSize: 5 })
    assert(text && text.includes(String(p.count)) && text.includes(String(p.required)), `${JSON.stringify(p)} ${hard}: ${text}`)
  }
  assert.equal(S.pendingText({ target: 'down', count: 2, required: 3 }), '2 of 3 consecutive failed checks; Down on the next failure')
  assert.equal(S.pendingText({ target: 'down', count: 1, required: 3 }), '1 of 3 consecutive failed checks towards Down')
  assert.equal(S.pendingText({ target: 'down', count: 4, required: 3 }), '4 consecutive failed checks (Down needs 3); Down is confirmed on the next failed check', 'after a policy decrease')
  assert.equal(S.pendingText({ target: 'degraded', count: 2, required: 3 }, { windowSize: 5 }), '2 checks of the last 5 slow, lossy or unanswered; Degraded at 3')
  assert.equal(S.pendingText({ target: 'up', count: 1, required: 2 }, { hardStatus: 'down' }), 'Responding for 1 of 2 checks needed to confirm recovery')
  assert.equal(S.pendingText({ target: 'up', count: 1, required: 3 }, { hardStatus: 'degraded' }), '1 of 3 consecutive checks within the recovery limits; Up at 3')
  assert.equal(S.pendingText(null), null)
  assert.equal(S.pendingText({ target: 'down', count: 0, required: 3 }), null)
  // The header chip is for a pending Down only.
  assert.equal(S.pendingDownChip({ target: 'down', count: 2, required: 3 }, 'up'), 'Down pending · 2 of 3 failed checks')
  assert.equal(S.pendingDownChip({ target: 'degraded', count: 2, required: 3 }, 'up'), null)
  assert.equal(S.pendingDownChip({ target: 'down', count: 2, required: 3 }, 'maintenance'), null)
}

// The rules in words, from the policy (not from constants).
{
  const s = S.policySentences(DEF, { packets: 3 })
  assert.equal(s.degraded, 'Degraded when 3 of the last 5 checks have latency above 100 ms, packet loss above 10% or no response (equal values are within the limit).')
  assert.equal(s.recovery, 'Back to Up from Degraded after 3 consecutive checks at or below the recovery limits (80 ms, 8%), or after 5 checks in a row with no slow, lossy or unanswered check.')
  assert.equal(s.down, 'Down after 3 consecutive failed checks (3 packets each); back to Up after the first responding check. A recovering device returns to Up first; Degraded is then confirmed on its own. Unanswered checks also count towards Degraded.')
  assert.match(s.flapping, /^Flapping when the status changes 4 times within 30 min .* It ends when at most 1 change remains in the window or after 10 min without a change\. While up\/down or latency\/loss changes flap, notifications for that kind of change are held: a status that lasts 10 min still notifies\. The other kind still notifies at once\.$/)
  const sensor = S.policySentences(DEF, { downConfirm: 1 })
  assert(sensor.down.startsWith('Down after 1 consecutive failed check;'), 'the owner\'s Down count')
  const l = S.policySentences(LEGACY)
  assert.equal(l.degraded, 'Degraded when a check\'s latency is above 100 ms or its packet loss above 10% (equal values are within the limit).')
  assert.equal(l.recovery, 'Back to Up from Degraded after one check at or below the Degraded limits.')
  assert(l.down.includes('It is Degraded straight away when that check is over the limits.'), 'the legacy preset reproduces down -> degraded')
  assert.equal(l.flapping, 'Flap detection is off: every confirmed status change notifies as usual.')
  const custom = S.policySentences(S.effectivePolicy({ degraded_rtt_ms: 500, degraded_loss_pct: 5, up_confirm_checks: 2, flap_stop_changes: 0 }))
  assert(custom.degraded.includes('above 500 ms, packet loss above 5%'))
  assert(custom.recovery.includes('(400 ms, 4%)'))
  assert(custom.down.includes('back to Up after 2 consecutive responding checks'))
  assert(custom.flapping.includes('It ends when no change remains in the window'))
  // Recovery from Degraded has two paths in the machine: degraded_clear_checks consecutive checks within the recovery
  // limits, or a full window without a slow, lossy or unanswered check (a check in the band counts towards that one).
  // The sentence names the paths that can decide.
  const rec = (raw) => S.policySentences(S.effectivePolicy(raw)).recovery
  assert.equal(rec({ degraded_confirm_checks: 1, degraded_clear_checks: 1 }),
    'Back to Up from Degraded after one check at or below the recovery limits (80 ms, 8%), or after 5 checks in a row with no slow, lossy or unanswered check.',
    'clear=1 with a 5-check window: both paths')
  assert.equal(rec({ degraded_confirm_checks: 1, degraded_window_checks: 1, degraded_clear_checks: 1 }),
    'Back to Up from Degraded after one check that is not slow, lossy or unanswered (latency at or below 100 ms, packet loss at or below 10%). The recovery limits (80 ms, 8%) have no effect: a window of 1 check is no longer than 1 consecutive recovery check.',
    '1 of 1 with auto limits: a 90 ms check recovers through the window, so the 80 ms limit never decides')
  const win1 = rec({ degraded_confirm_checks: 1, degraded_window_checks: 1, degraded_clear_checks: 3 })
  assert(!win1.includes('1 checks') && win1.startsWith('Back to Up from Degraded after one check that is not slow'), win1)
  assert.equal(rec({ degraded_confirm_checks: 2, degraded_window_checks: 4, degraded_clear_checks: 10 }),
    'Back to Up from Degraded after 4 checks in a row with no slow, lossy or unanswered check (latency at or below 100 ms, packet loss at or below 10%). The recovery limits (80 ms, 8%) have no effect: a window of 4 checks is no longer than 10 consecutive recovery checks.')
  assert.equal(rec({ degraded_confirm_checks: 1, degraded_window_checks: 4, degraded_clear_checks: 2, degraded_rtt_clear_ms: 100, degraded_loss_clear_pct: 10 }),
    'Back to Up from Degraded after 2 consecutive checks at or below the Degraded limits.', 'no band: both paths need the same checks, the faster count wins')
  assert.equal(rec({ degraded_confirm_checks: 1, degraded_window_checks: 2, degraded_clear_checks: 5, degraded_rtt_clear_ms: 100, degraded_loss_clear_pct: 10 }),
    'Back to Up from Degraded after 2 consecutive checks at or below the Degraded limits.')
}

// Timing and the longest interval at which flapping can start (changes must fall strictly inside the window).
{
  assert.deepEqual(S.maxDetectableIntervals(DEF), { reachability: 359, performance: 199, sensorReachability: 599 },
    'up/down: changes at checks 3,4,7,8 span 5 intervals < 30 min; latency/loss: 3 confirm + 3 clear alternating span 9')
  assert.equal(S.flapSpanChecks(4, 3, 1), 5)
  assert.equal(S.flapSpanChecks(4, 3, 3), 9)
  assert.equal(S.flapSpanChecks(3, 3, 1), 4)
  assert.equal(S.maxDetectableIntervals(S.effectivePolicy({ flap_start_changes: 3 })).reachability, 449)
  assert.equal(S.maxDetectableIntervals(S.effectivePolicy({ degraded_clear_checks: 10, degraded_window_checks: 4, degraded_confirm_checks: 2 })).performance, 1800 / 8 - 1, 'the clean-window path is faster than 10 clear checks')
  const rows = S.timingRows(DEF)
  assert.deepEqual(rows.map((r) => r.intervalSeconds), [30, 60, 300])
  assert.deepEqual(rows[0], { intervalSeconds: 30, down: '1 min–1 min 30 s', up: 'within 30 s', degraded: '1 min–1 min 30 s', degradedClear: '1 min–1 min 30 s', flap: 'both' })
  assert.deepEqual(rows[1], { intervalSeconds: 60, down: '2–3 min', up: 'within 1 min', degraded: '2–3 min', degradedClear: '2–3 min', flap: 'both' })
  assert.deepEqual(rows[2], { intervalSeconds: 300, down: '10–15 min', up: 'within 5 min', degraded: '10–15 min', degradedClear: '10–15 min', flap: 'reachability' })
  assert(S.timingRows(LEGACY).every((r) => r.flap === 'none'), 'no flapping with detection off')
  assert.deepEqual(S.timingRows(S.effectivePolicy({ degraded_clear_checks: 10, degraded_window_checks: 4, degraded_confirm_checks: 2 })).map((r) => r.degradedClear),
    ['1 min 30 s–2 min', '3–4 min', '15–20 min'], 'a 4-check clean window recovers before 10 clear checks (as maxDetectableIntervals counts it)')
  assert.equal(S.TIMING_FLAP_LABEL.reachability, 'Up/down only')
  assert.equal(S.confirmationDelay(2, 20), '20–40 s')
  assert.equal(S.flapDetectionLimitText(DEF), 'Flapping can start only on devices polled at intervals of up to 5 min 59 s for up/down changes (9 min 59 s on remote sensors) and up to 3 min 19 s for latency/loss changes; slower devices cannot reach 4 confirmed changes within 30 min.')
  assert.equal(S.flapDetectionLimitText(LEGACY), 'Flap detection is off.')
  // The 3-packet granularity.
  assert.equal(S.lossGranularityNote(DEF), 'Each check sends 3 packets, so its loss is 0%, 33.3%, 66.7% or 100%. With the 10% limit, a check is lossy when one packet is lost. A recovery limit below 33.3% means a check counts towards the consecutive recovery checks only with no loss at all.')
  assert.match(S.lossGranularityNote(S.effectivePolicy({ degraded_loss_pct: 50, degraded_loss_clear_pct: 40 })), /lossy when 2 of 3 packets are lost\.$/)
  assert.equal(S.packetsLostToExceed(100), 4, 'a 100% limit is never exceeded')
}

// The settings form: GET (stored over defaults + effective) -> form -> the FULL document for PUT.
{
  const get = { ...vectors.policy_cases[0].expect_effective, degraded_rtt_clear_ms: null, degraded_loss_clear_pct: null, degraded_rtt_ms: 500, degraded_loss_pct: 5, effective: { degraded_rtt_clear_ms: 400 } }
  const form = S.formFromSettings(get)
  assert.equal(form.rtt_clear_override, false, 'a null recovery limit is derived')
  const payload = S.settingsPayload(form)
  assert.deepEqual(Object.keys(payload), [...S.POLICY_KEYS], 'every field, every time, in the contract order')
  assert.equal(Object.keys(payload).length, 15)
  assert.equal(payload.degraded_rtt_clear_ms, null)
  assert.equal(payload.degraded_loss_clear_pct, null)
  assert.equal(payload.degraded_rtt_ms, 500)
  assert.equal(payload.flap_detection_enabled, true)
  assert(Object.values(payload).every((v) => v === null || typeof v === 'boolean' || (typeof v === 'number' && Number.isFinite(v))), 'numbers are numbers')
  assert.deepEqual(S.validateMonitoringForm(form), {})
  // An older server answers only the two limits: the rest are the defaults, and Save still sends all of them.
  const old = S.settingsPayload(S.formFromSettings({ degraded_rtt_ms: 250, degraded_loss_pct: 20 }))
  assert.deepEqual(old, { ...S.SETTINGS_DEFAULTS, degraded_rtt_ms: 250, degraded_loss_pct: 20 })
  // A stored override round-trips.
  const kept = S.formFromSettings({ ...S.SETTINGS_DEFAULTS, degraded_rtt_clear_ms: 70 })
  assert.equal(kept.rtt_clear_override, true)
  assert.equal(S.settingsPayload(kept).degraded_rtt_clear_ms, 70)
  // The derived limit the card shows is the server's (round3), also while typing.
  assert.equal(S.policyFromForm({ ...S.defaultsForm(), degraded_rtt_ms: '1.005625' }).degraded_rtt_clear_ms, 0.804)
  // Restore defaults and the legacy preset.
  assert.deepEqual(S.settingsPayload(S.defaultsForm()), S.SETTINGS_DEFAULTS)
  const legacy = S.settingsPayload(S.legacyPresetForm(S.formFromSettings(get)))
  assert.deepEqual(legacy, { ...S.settingsPayload(form), degraded_confirm_checks: 1, degraded_window_checks: 1, degraded_clear_checks: 1, degraded_rtt_clear_ms: 500, degraded_loss_clear_pct: 5, flap_detection_enabled: false },
    'one check confirms and clears, no hysteresis band, no flap detection; Down/Up counts kept')
  assert.equal(S.isLegacyPolicy(S.effectivePolicy(legacy)), true)
  assert.equal(S.isLegacyPolicy(DEF), false)
}

// Client validation mirrors the server's 422 rules for the merged (here: full) document.
{
  const base = S.defaultsForm()
  const errs = (patch) => S.validateMonitoringForm({ ...base, ...patch })
  assert.deepEqual(errs({}), {})
  assert(errs({ degraded_rtt_ms: '0' }).degraded_rtt_ms)
  assert(errs({ degraded_rtt_ms: '10001' }).degraded_rtt_ms)
  assert(errs({ degraded_rtt_ms: '' }).degraded_rtt_ms, 'empty is not a number')
  assert.deepEqual(errs({ degraded_rtt_ms: '10000' }), {})
  assert(errs({ degraded_loss_pct: '0' }).degraded_loss_pct, 'loss is at least 1%')
  assert(errs({ degraded_loss_pct: '101' }).degraded_loss_pct)
  assert(errs({ rtt_clear_override: true, degraded_rtt_clear_ms: '120' }).degraded_rtt_clear_ms, 'recovery latency <= Degraded latency')
  assert(errs({ rtt_clear_override: true, degraded_rtt_clear_ms: '0' }).degraded_rtt_clear_ms, 'recovery latency > 0')
  assert.deepEqual(errs({ rtt_clear_override: true, degraded_rtt_clear_ms: '100' }), {}, 'equal to the limit is allowed (no band)')
  assert(errs({ loss_clear_override: true, degraded_loss_clear_pct: '11' }).degraded_loss_clear_pct)
  assert.deepEqual(errs({ loss_clear_override: true, degraded_loss_clear_pct: '0' }), {}, 'zero loss is a valid recovery limit')
  assert.deepEqual(errs({ rtt_clear_override: false, degraded_rtt_clear_ms: 'garbage' }), {}, 'a derived limit is not validated')
  assert(errs({ degraded_confirm_checks: '6', degraded_window_checks: '5' }).degraded_window_checks, 'window >= confirm')
  assert(errs({ degraded_confirm_checks: '2.5' }).degraded_confirm_checks, 'whole numbers only')
  assert(errs({ degraded_confirm_checks: '21' }).degraded_confirm_checks)
  assert(errs({ down_confirm_checks: '11' }).down_confirm_checks)
  assert(errs({ sensor_down_confirm_checks: '0' }).sensor_down_confirm_checks)
  assert(errs({ up_confirm_checks: '11' }).up_confirm_checks)
  assert(errs({ degraded_clear_checks: '0' }).degraded_clear_checks)
  assert(errs({ flap_window_minutes: '4' }).flap_window_minutes)
  assert(errs({ flap_window_minutes: '1441' }).flap_window_minutes)
  assert(errs({ flap_start_changes: '2' }).flap_start_changes)
  assert(errs({ flap_start_changes: '65' }).flap_start_changes, 'the 64-change history cap')
  assert(errs({ flap_start_changes: '4', flap_stop_changes: '4' }).flap_stop_changes, 'stop <= start - 1')
  assert.deepEqual(errs({ flap_start_changes: '4', flap_stop_changes: '3' }), {})
  assert(errs({ flap_stable_minutes: '31' }).flap_stable_minutes, 'stable <= flap window')
  assert(errs({ flap_stable_minutes: '0' }).flap_stable_minutes)
}

// Durations, badges and tooltips.
{
  assert.equal(S.formatSpan(2 * 3600 + 14 * 60 + 5), '2h 14m')
  assert.equal(S.formatSpan(45), '45s')
  assert.equal(S.formatSpan(3 * 86400 + 4 * 3600), '3d 4h')
  assert.equal(S.formatSpan(7200), '2h')
  assert.equal(S.statusForText(iso(-134), T0), 'for 2h 14m')
  assert.equal(S.statusForText('2026-10-14 09:46:00', T0), 'for 2h 14m', 'a naive timestamp is UTC')
  assert.equal(S.statusForText(null, T0), null)
  assert.equal(S.statusForText(iso(5), T0), 'for 0s', 'clock skew never prints a negative age')
  assert.equal(S.fmtSeconds(90), '1 min 30 s')
  assert.equal(S.fmtSeconds(3900), '1 h 5 min')
  assert.equal(S.isFlapping({ is_flapping: true }), true)
  assert.equal(S.isFlapping({ is_flapping: null }), false)
  assert.equal(S.isFlapping({}), false, 'an older server has no flag')
  assert.equal(S.flapBadgeText('reachability'), 'Flapping')
  assert.equal(S.flapBadgeText('performance', true), 'Flapping · latency/loss')
  assert.equal(S.flapBadgeText('bogus', true), 'Flapping')
  const tip = S.flapTooltip({ kind: 'reachability', since: iso(-38), changes: 5, windowMinutes: 30, stableMinutes: 10, earliestStopAt: iso(14) }, T0, utc)
  assert.equal(tip, 'Flapping (up/down changes) since 11:22 (38m). 5 status changes in the last 30 min. Notifications for changes into or out of Down are held: a status that lasts 10 min still notifies. Changes between Up and Degraded still notify at once. Ends 12:14 at the earliest if the status stops changing.')
  assert.equal(S.flapTooltip({}, T0, utc), 'Flapping. Notifications for the kind of change that is flapping are held until a status lasts the flap stability time.')
  // The engine holds a trigger only while ITS class flaps (alert_engine flap_held = event.flapping): a latency/loss
  // flapper still pages a Down at once, an up/down flapper still pages Up -> Degraded at once.
  assert.equal(S.flapTooltip({ kind: 'performance', stableMinutes: 10 }, T0, utc), 'Flapping (latency/loss changes). Notifications for changes between Up and Degraded are held: a status that lasts 10 min still notifies. Changes into or out of Down still notify at once.')
  assert.equal(S.flapTooltip({ kind: 'both', stableMinutes: 10 }, T0, utc), 'Flapping (up/down and latency/loss changes). Status notifications for this device are held: a status that lasts 10 min still notifies.')
  assert.equal(S.flapHeldNoun('reachability'), 'up/down notifications')
  assert.equal(S.flapHeldNoun('performance'), 'latency/loss notifications')
  assert.equal(S.flapHeldNoun('both'), 'status notifications')
  assert.equal(S.flapHeldNoun(null), 'notifications for the flapping changes')
  assert(!S.flapHeldText('performance').includes('Status notifications'), 'a latency/loss episode never claims every status notification')
  assert.equal(S.flapChangesInWindow({ kind: 'both', reach_changes_in_window: 4, perf_changes_in_window: 5 }), 9)
  assert.equal(S.flapChangesInWindow({ kind: 'performance', reach_changes_in_window: 1, perf_changes_in_window: 5 }), 5)
}

// Fleet ranking: flapping is a problem of its own rank, and "All devices healthy" never shows while a device flaps.
{
  assert.equal(S.problemSeverity({ status: 'down', is_flapping: true }, 0), 3)
  assert.equal(S.problemSeverity({ status: 'up', is_flapping: true }, 0), 2)
  assert.equal(S.problemSeverity({ status: 'degraded' }, 0), 2)
  assert.equal(S.problemSeverity({ status: 'up' }, 2), 1)
  assert.equal(S.problemSeverity({ status: 'up', is_flapping: false }, 0), 0)
  assert.equal(S.attentionEmptyText(0), 'All devices healthy 🎉')
  assert.equal(S.attentionEmptyText(undefined), 'All devices healthy 🎉', 'an older summary without the count')
  assert.equal(S.attentionEmptyText(2), '2 devices flapping (notifications for the flapping changes held)')
}

// Alerts: held while flapping, and the two notices.
{
  const held = S.alertFlapState({ status: 'pending', metadata: { flap_held: true, flap_kind: 'reachability', hold_until: iso(10) } }, utc)
  assert.equal(held.kind, 'held')
  assert.equal(held.label, 'Held: device flapping')
  assert.equal(held.until, iso(10))
  assert.equal(held.title, 'Held because the device is flapping: sent at 12:10 if the device is still in this status, otherwise closed without a notification.')
  assert.equal(S.alertFlapState({ status: 'active', metadata: { flap_held: true } }), null, 'a confirmed (dispatched) hold is a normal alert')
  assert.equal(S.alertFlapState({ status: 'pending', metadata: {} }), null, 'an ordinary rule hold')
  assert.equal(S.alertFlapState({ status: 'pending' }), null)
  assert.equal(S.alertFlapState({ status: 'resolved', metadata: { flap_notice: true, flap_event: 'start' } }).label, 'Flapping started')
  assert.equal(S.alertFlapState({ status: 'resolved', metadata: { flap_notice: true, flap_event: 'stop' } }).label, 'Flapping stopped')
  // A notice names the kind of change whose notifications are held (device_flap_notice_service: metadata.kind, stable_minutes).
  const notice = (meta) => S.alertFlapState({ status: 'resolved', metadata: { flap_notice: true, ...meta } })
  assert.equal(notice({ flap_event: 'start', kind: 'performance', stable_minutes: 10 }).detail,
    'This notice opens no incident. Notifications for changes between Up and Degraded are held: a status that lasts 10 min still notifies. Changes into or out of Down still notify at once.')
  assert.equal(notice({ flap_event: 'start', kind: 'reachability', stable_minutes: 10 }).title,
    'Notice: the device started flapping (up/down changes). Notifications for changes into or out of Down are held: a status that lasts 10 min still notifies. Changes between Up and Degraded still notify at once.')
  assert.equal(notice({ flap_event: 'start' }).detail, 'This notice opens no incident. Notifications for the kind of change that is flapping are held until a status lasts the flap stability time.')
  assert.equal(notice({ flap_event: 'stop', kind: 'performance' }).detail, 'This notice opens no incident. Latency/loss notifications for the device have resumed.')
  assert.equal(notice({ flap_event: 'stop', kind: 'both' }).title, 'Notice: the device stopped flapping (up/down and latency/loss changes). Status notifications for the device have resumed.')
  // The row as alerts.serialize sends it: top-level flap_held/hold_until (pending rows only) beside the full metadata.
  const row = S.alertFlapState({ status: 'pending', flap_held: true, hold_until: iso(10), metadata: {} }, utc)
  assert.equal(row.kind, 'held', 'the top-level flag alone marks the hold')
  assert.equal(row.until, iso(10))
  assert.equal(S.alertFlapState({ status: 'pending', flap_held: false, hold_until: iso(5), metadata: {} }), null, 'an ordinary rule hold with hold_until')
  assert.equal(S.alertFlapState({ status: 'pending', flap_held: true, hold_until: null, metadata: { hold_until: iso(7) } }, utc).until, iso(7), 'metadata fills an absent top-level hold end')
  assert.equal(S.alertFlapState({ status: 'acknowledged', flap_held: false, metadata: { flap_held: true } }), null)
}

// The activity log: the changes inside an episode (and the ones that started it) become one row.
{
  const h = (min, from, to) => ({ timestamp: iso(min), old_status: from, new_status: to, reason: 'x' })
  const history = [
    h(-60, 'up', 'down'), // a real outage long before the episode
    h(-59, 'down', 'up'),
    h(-20, 'up', 'down'), h(-19, 'down', 'up'), h(-15, 'up', 'down'), h(-14, 'down', 'up'), // 4 changes: flapping starts at -14
    h(-12, 'up', 'degraded'), // a latency change during a reachability episode stays its own row
    h(-10, 'degraded', 'up'),
    h(-8, 'up', 'down'), h(-7, 'down', 'up'),
    h(5, 'up', 'maintenance'), // never collapsed
  ]
  const ep = { id: 'e1', started_at: iso(-14), ended_at: iso(3), kind: 'reachability', start_changes: 4, window_minutes: 30, status_at_end: 'up', end_reason: 'stable' }
  assert.equal(S.transitionClass('up', 'down'), 'reachability')
  assert.equal(S.transitionClass('degraded', 'up'), 'performance')
  assert.equal(S.transitionClass('unknown', 'up'), null)
  assert.equal(S.transitionClass('up', 'maintenance'), null)
  const groups = S.groupFlapTransitions(history, [ep], T0)
  const episodeRows = groups.filter((g) => g.kind === 'episode')
  assert.equal(episodeRows.length, 1)
  assert.deepEqual(episodeRows[0].items.map((t) => t.timestamp), [iso(-20), iso(-19), iso(-15), iso(-14), iso(-8), iso(-7)], 'the starting changes and the episode\'s own, same class only')
  assert.equal(groups.filter((g) => g.kind === 'transition').length, 5, 'the old outage, the latency pair and maintenance stay separate')
  // An earlier change inside the lead-in window but outside it (more than 30 min before the start) stays separate.
  assert(!S.episodeCovers(ep, h(-44, 'up', 'down'), T0))
  assert(S.episodeCovers(ep, h(-43, 'up', 'down'), T0))
  // An open episode covers until now; 'both' covers both classes.
  assert(S.episodeCovers({ ...ep, ended_at: null }, h(30, 'up', 'down'), T0 + 31 * 60_000))
  assert(S.episodeCovers({ ...ep, kind: 'both' }, h(-12, 'up', 'degraded'), T0))
  const summary = S.episodeSummary(ep, episodeRows[0].items, T0, utc)
  assert.equal(summary.title, 'Flapping (up/down): 6 status changes')
  // The span is the flapping itself; the lead-in changes (notified as usual) are mentioned on their own.
  assert.equal(summary.subtitle, '11:46–12:03 · 3 earlier changes from 11:40 led to it · ended (status stable) · up/down notifications held while flapping')
  assert.equal(summary.timestamp, iso(3), 'sorted by its last activity')
  assert.equal(S.episodeSummary({ ...ep, ended_at: null }, [], T0, utc).subtitle, '11:46–now · ongoing · up/down notifications held while flapping')
  assert.equal(S.episodeSummary({ ...ep, kind: 'performance' }, [h(-14, 'up', 'degraded')], T0, utc).subtitle, '11:46–12:03 · ended (status stable) · latency/loss notifications held while flapping')
  // The full log keeps every change, marked with its episode and phase: before the start the class was not flapping
  // yet (the transition flag is false, its Down paged), so those changes only led to the episode.
  const full = S.groupFlapTransitions(history, [ep], T0, false)
  assert.equal(full.length, history.length)
  assert.equal(full.filter((g) => g.episode).length, 6)
  const phaseAt = (min) => full.find((g) => g.item.timestamp === iso(min)).phase
  assert.deepEqual([-20, -19, -15].map(phaseAt), ['lead-in', 'lead-in', 'lead-in'], 'the changes before started_at')
  assert.equal(phaseAt(-14), 'during', 'the change at started_at made the class flap and carries the flag')
  assert.deepEqual([-8, -7].map(phaseAt), ['during', 'during'])
  assert.equal(phaseAt(-60), null)
  assert.equal(S.flapPhase({ ...ep, started_at: '2026-10-14T11:46:00.000123+00:00' }, h(-14, 'up', 'down')), 'during', 'sub-millisecond differences compare equal')
  assert.equal(S.groupFlapTransitions(history, [], T0).length, history.length, 'no episodes: nothing changes')
}

// The explanation: copy from the real policy and state; active = degraded || down || pending || flapping.
const FORBIDDEN = [
  'Degraded when a responding ping check',
  'A responding check resets the failure sequence and clears Down',
  'latency or packet loss can still mark the device Degraded',
  'Isolated failures remain diagnostic evidence',
  'The reported status may be awaiting refresh',
  'status may be awaiting refresh',
  'Status uses individual checks',
  'Each ping check is evaluated individually',
]
const explanation = (over = {}) => ({
  status: 'up', mode: 'ping', source: 'Controller',
  thresholds: { degraded_rtt_ms: 100, degraded_loss_pct: 10, clear_rtt_ms: 80, clear_loss_pct: 8 },
  confirmation: { failed_checks: 3, packets_per_check: 3, interval_seconds: 60, recovery_checks: 1 },
  policy: { ...DEF, scope: 'global' },
  state: { hard_status: 'up', in_maintenance: false, pending: null, fail_streak: 0, ok_streak: 0, clear_streak: 0, bad_in_window: 0, window_len: 5, window_size: 5, status_since: iso(-134) },
  flapping: { active: false, kind: null, since: null, reach_changes_in_window: 0, perf_changes_in_window: 0, window_minutes: 30, start_changes: 4, stop_changes: 1, stable_minutes: 10, enabled: true, earliest_stop_at: null, max_detectable_interval_s: 359 },
  latest: { timestamp: iso(-1), rtt_ms: 12, packet_loss_pct: 0, is_up: true, stale: false, settings_pending: false, breaches: [] },
  recent_degraded: null,
  ...over,
})
const generated = []
const view = (data) => { const v = S.explanationView(data, T0, utc); generated.push(JSON.stringify(v)); return v }
{
  const calm = view(explanation())
  assert.equal(calm.active, false)
  assert.equal(calm.heading, 'Reachability status details')
  assert.equal(calm.statusLine, 'Up for 2h 14m (since 09:46)')
  assert.equal(calm.scopeNote, 'Global policy')
  assert.equal(calm.summary, 'Last check 11:59 · Responded')
  assert.deepEqual(calm.rules, Object.values(S.policySentences(DEF, { downConfirm: 3, packets: 3 })))
  assert.equal(S.latestVerdict(explanation().latest, explanation()).text, 'Both measurements are within the Degraded limits.')
  assert.equal(S.limitText('latency', explanation()), '100 ms limit')

  // Pending Down: active, with progress, not a status of its own.
  const pending = view(explanation({ state: { ...explanation().state, pending: { target: 'down', count: 2, required: 3 }, fail_streak: 2 }, latest: { ...explanation().latest, is_up: false, rtt_ms: 0, packet_loss_pct: 100 } }))
  assert.equal(pending.active, true)
  assert.equal(pending.tone, 'warning')
  assert.equal(pending.heading, 'Status change pending')
  assert.equal(pending.pending, '2 of 3 consecutive failed checks; Down on the next failure')
  assert.equal(pending.summary, '2 of 3 consecutive failed checks; Down on the next failure')

  // Down, recovering with up_confirm 2: the owner's counts and the recovery progress.
  const downData = explanation({ status: 'down', policy: { ...DEF, up_confirm_checks: 2, scope: 'global' }, state: { ...explanation().state, hard_status: 'down', pending: { target: 'up', count: 1, required: 2 }, ok_streak: 1, fail_streak: 0 } })
  const down = view(downData)
  assert.equal(down.heading, 'Why this device is down')
  assert.equal(down.tone, 'danger')
  assert.equal(down.pending, 'Responding for 1 of 2 checks needed to confirm recovery')
  assert(down.rules[2].includes('back to Up after 2 consecutive responding checks'))

  // Degraded with a check between the limits: both limits shown, and why it is still Degraded.
  const degradedData = explanation({ status: 'degraded', state: { ...explanation().state, hard_status: 'degraded', clear_streak: 0, bad_in_window: 2 }, latest: { ...explanation().latest, rtt_ms: 95 } })
  const degraded = view(degradedData)
  assert.equal(degraded.heading, 'Why this device is degraded')
  assert.equal(degraded.active, true)
  assert.equal(S.limitText('latency', degradedData), 'Degraded above 100 ms · recovers at or below 80 ms', 'both limits while degraded')
  assert.equal(S.limitText('loss', degradedData), 'Degraded above 10% · recovers at or below 8%')
  // A check in the band restarts the consecutive recovery checks but counts towards the clean-window recovery
  // (device_status_machine.step: 150,150,150 then 90 ms x5 recovers on the window path).
  assert.equal(S.latestVerdict(degradedData.latest, degradedData).text, 'Within the Degraded limits but above a recovery limit (80 ms, 8%): this check does not degrade and restarts the count of 3 consecutive checks within the recovery limits, but it counts towards recovery after 5 checks in a row with no slow, lossy or unanswered check.')
  const clear1 = { ...degradedData, policy: { ...DEF, degraded_clear_checks: 1, scope: 'global' } }
  assert.equal(S.latestVerdict(clear1.latest, clear1).text, 'Within the Degraded limits but above a recovery limit (80 ms, 8%): this check does not degrade and is not a recovery check on its own, but it counts towards recovery after 5 checks in a row with no slow, lossy or unanswered check.')
  const clearing = { ...degradedData, latest: { ...degradedData.latest, rtt_ms: 40 }, state: { ...degradedData.state, clear_streak: 1, pending: { target: 'up', count: 1, required: 3 } } }
  assert.equal(S.latestVerdict(clearing.latest, clearing).text, 'Both measurements are at or below the recovery limits (80 ms, 8%); this check counts towards recovery.')
  assert.equal(view(clearing).pending, '1 of 3 consecutive checks within the recovery limits; Up at 3')
  // The canonical predicates: equal is within; float32 loss at the limit is not a breach.
  assert.equal(S.classifyCheck({ is_up: true, rtt_ms: 100, packet_loss_pct: 10.000000149 }, DEF), 'marginal')
  assert.equal(S.classifyCheck({ is_up: true, rtt_ms: 100.00001, packet_loss_pct: 0 }, DEF), 'breach')
  assert.equal(S.classifyCheck({ is_up: true, rtt_ms: 80, packet_loss_pct: 0 }, DEF), 'clear')
  assert.equal(S.latestVerdict({ ...degradedData.latest, rtt_ms: 120, breaches: [{ metric: 'Latency' }] }, degradedData), null, 'breaches are listed instead')
  assert.match(S.latestVerdict({ ...degradedData.latest, stale: true }, degradedData).text, /overdue/)

  // Flapping on an Up device.
  const flapData = explanation({ flapping: { ...explanation().flapping, active: true, kind: 'reachability', since: iso(-38), reach_changes_in_window: 5, earliest_stop_at: iso(14) } })
  const flap = view(flapData)
  assert.equal(flap.active, true)
  assert.equal(flap.heading, 'Why this device is flapping')
  assert.equal(flap.flapping, 'Flapping (up/down changes) since 11:22. 5 confirmed changes in the last 30 min (flapping starts at 4). It ends after 10 min without a change (12:14 at the earliest) or when at most 1 change remains in the window. Notifications for changes into or out of Down are held: a status that lasts 10 min still notifies. Changes between Up and Degraded still notify at once. Interface, service and metric alerts are not affected.')
  assert.equal(flap.summary, 'Flapping (up/down); only up/down notifications held')
  const perfFlap = view(explanation({ flapping: { ...explanation().flapping, active: true, kind: 'performance', since: iso(-10), perf_changes_in_window: 4 } }))
  assert(perfFlap.flapping.includes('Notifications for changes between Up and Degraded are held: a status that lasts 10 min still notifies. Changes into or out of Down still notify at once.'), perfFlap.flapping)
  assert.equal(perfFlap.summary, 'Flapping (latency/loss); only latency/loss notifications held')
  assert.equal(view(explanation({ flapping: { ...explanation().flapping, active: true, kind: 'both' } })).summary, 'Flapping (up/down and latency/loss); status notifications held')
  assert.equal(view(explanation({ flapping: { ...explanation().flapping, active: true, kind: null } })).summary, 'Flapping; notifications for the flapping changes held')
  assert.equal(view({ ...flapData, status: 'down', state: { ...flapData.state, hard_status: 'down' } }).heading, 'Why this device is down and flapping')

  // Pending Degraded counts the checks actually in the window (cleared by the last confirmed change).
  const young = view(explanation({ state: { ...explanation().state, pending: { target: 'degraded', count: 2, required: 3 }, bad_in_window: 2, window_len: 3, window_size: 5 } }))
  assert.equal(young.pending, '2 checks of the last 3 slow, lossy or unanswered; Degraded at 3')
  assert.equal(young.tone, 'info', 'a pending Degraded is no warning of its own')

  // A long silence: nothing pending, and the counts start over at the next check.
  const silent = view(explanation({ state: { ...explanation().state, stale: true, pending: null } }))
  assert.equal(silent.stateNote, 'No check for more than 3 min: the next check starts the confirmation counts over.')
  assert.equal(silent.active, false)
  assert.equal(view(explanation({ state: { ...explanation().state, seeded: true } })).stateNote, 'The confirmed status is carried over from the device record; the confirmation counts start with the next check.')
  assert.equal(calm.stateNote, null)

  // The server's own classification of the latest check wins over the browser's (same predicates, server values).
  assert.equal(S.latestVerdict({ ...degradedData.latest, rtt_ms: 40, classification: 'marginal' }, degradedData).tone, 'muted')
  // An older server's thresholds under either spelling.
  assert.equal(S.explanationPolicy({ status: 'up', mode: 'ping', thresholds: { degraded_rtt_ms: 200, degraded_loss_pct: 10, degraded_rtt_clear_ms: 150 } }).degraded_rtt_clear_ms, 150)
  assert.equal(S.explanationPolicy({ status: 'up', mode: 'ping', thresholds: { degraded_rtt_ms: 200, degraded_loss_pct: 10, clear_rtt_ms: 120 } }).degraded_rtt_clear_ms, 120)

  // A device polled too slowly for flap detection is told so.
  const slow = view(explanation({ confirmation: { ...explanation().confirmation, interval_seconds: 600 } }))
  assert.equal(slow.flapLimit, 'At this device\'s 10 min ping interval flapping cannot be detected (it needs an interval of 5 min 59 s or less).')
  // The server's `detectable` decides when it sends one.
  assert.equal(view(explanation({ flapping: { ...explanation().flapping, detectable: true }, confirmation: { ...explanation().confirmation, interval_seconds: 600 } })).flapLimit, null)
  assert.match(view(explanation({ flapping: { ...explanation().flapping, detectable: false, max_detectable_interval_s: 199 }, confirmation: { ...explanation().confirmation, interval_seconds: 300 } })).flapLimit, /5 min ping interval flapping cannot be detected \(it needs an interval of 3 min 19 s or less\)/)
  assert.equal(view(explanation({ policy: { ...LEGACY, scope: 'global' }, confirmation: { ...explanation().confirmation, interval_seconds: 600 } })).flapLimit, null, 'nothing to say with detection off')

  // A sensor-owned device: the explanation uses the owner's Down count.
  const sensor = view(explanation({ source: 'branch-sensor', confirmation: { failed_checks: 1, packets_per_check: 3, interval_seconds: 60, recovery_checks: 1 } }))
  assert(sensor.rules[2].startsWith('Down after 1 consecutive failed check'))

  // Maintenance: the overlay and the confirmed status underneath.
  const maint = view(explanation({ status: 'maintenance', state: { ...explanation().state, hard_status: 'down', in_maintenance: true } }))
  assert.equal(maint.maintenance, 'A maintenance window is active. Checks keep being evaluated (confirmed status: Down); the confirmed status is applied, with its notifications, when the window ends.')
  assert.equal(maint.statusLine, null)

  // An older server (no policy/state/flapping): the legacy thresholds over the defaults, nothing invented.
  const legacyServer = view({ status: 'degraded', mode: 'ping', source: 'Controller', thresholds: { degraded_rtt_ms: 250, degraded_loss_pct: 20 }, latest: null })
  assert.equal(legacyServer.heading, 'Why this device is degraded')
  assert(legacyServer.rules[0].includes('above 250 ms, packet loss above 20%'))
  assert.equal(legacyServer.pending, null)
  assert.equal(legacyServer.summary, 'No recent check available')

  // Legacy single-check policy in the explanation.
  const legacyPolicy = view(explanation({ policy: { ...LEGACY, scope: 'global' } }))
  assert(legacyPolicy.rules[0].startsWith('Degraded when a check\'s latency is above'))
}

/* ═══ (b) the false sentences are gone ══════════════════════════════════════════════════════════ */

{
  const copy = generated.join('\n') + Object.values(S.policySentences(DEF)).join('\n') + Object.values(S.policySentences(LEGACY)).join('\n')
  for (const sentence of FORBIDDEN) assert(!copy.includes(sentence), `generated copy: ${sentence}`)
  const explanationSrc = fs.readFileSync('src/components/devices/DeviceStatusExplanation.tsx', 'utf8')
  for (const sentence of FORBIDDEN) assert(!explanationSrc.includes(sentence), `DeviceStatusExplanation.tsx: ${sentence}`)
  assert(!/\/ \{number\(data\.thresholds\.degraded_rtt_ms\)\} ms limit/.test(explanationSrc), 'no single "/ X ms limit" while degraded')
  assert(explanationSrc.includes('explanationView(data, serverNow())') && explanationSrc.includes("limitText('latency', data)"), 'the copy comes from the policy and state')
  assert(!explanationSrc.includes("data.status === 'degraded'\n"), 'active is not degraded-only')

  const settings = fs.readFileSync('src/pages/GeneralSettingsPage.tsx', 'utf8')
  const card = settings.slice(settings.indexOf('export function MonitoringCard('), settings.indexOf('function AppearanceCard('))
  for (const sentence of FORBIDDEN) assert(!card.includes(sentence), `MonitoringCard: ${sentence}`)
  assert(!card.includes('Degraded thresholds'), 'the card is no longer about single-check thresholds')
  assert(card.includes('save.mutate(settingsPayload(f))'), 'Save sends the full document')
  assert(!/api\.put\('\/settings\/monitoring', \{/.test(card), 'no hand-picked partial payload')
  for (const text of ['Restore defaults', 'Legacy single-check preset', 'Advanced: recovery and flapping', 'What changed:', 'lossGranularityNote(policy)', 'flapDetectionLimitText(policy)', 'timingRows(policy)', 'validateMonitoringForm(f)']) {
    assert(card.includes(text), `MonitoringCard: ${text}`)
  }

  const detail = fs.readFileSync('src/pages/DeviceDetailPage.tsx', 'utf8')
  assert(!detail.includes('Last device check'), 'renamed to Last response')
  assert(detail.includes('Last response:'))
  assert(!detail.includes('Last reported reachability'), 'the pill is the confirmed status')
  assert(detail.includes('statusForText(device.status_changed_at, now)') && detail.includes('pendingDownChip(') && detail.includes('<FlappingTag'))
  assert(detail.includes('buildActivityEvents(traps || [], statusHistory || [], metrics || {}, range.fromISO, range.toISO, flapEpisodes || [])'), 'the widget collapses episodes')
  assert(detail.includes('episodes || [], false)'), 'the full log keeps every change')

  // A flap hold covers only the class that flaps: no surface claims every status notification of a flapping device.
  const UNQUALIFIED_HOLD = [
    'During flapping (status notifications held)', 'their status notifications are held', 'Their status notifications are held',
    'Status notifications for the device are held while it flaps', 'status notifications of flapping devices are held',
    'While flapping, a status notifies only once', 'its status alerts are also held automatically', "'status notifications held while flapping'",
    'neither degrades nor counts towards recovery',
  ]
  for (const file of ['src/pages/DeviceDetailPage.tsx', 'src/pages/DevicesPage.tsx', 'src/components/availability/views.tsx', 'src/pages/AlertDetailPage.tsx', 'src/pages/GeneralSettingsPage.tsx', 'src/components/forms/AlertRuleWizardDialog.tsx', 'src/components/devices/deviceStatus.ts']) {
    const text = fs.readFileSync(file, 'utf8')
    for (const sentence of UNQUALIFIED_HOLD) assert(!text.includes(sentence), `${file}: ${sentence}`)
  }
  assert(detail.includes("phase === 'lead-in' ? 'Led to flapping'") && detail.includes('transitionEvent(g.item, i, g.phase)'), 'the full log marks lead-in changes without a hold claim')
  assert(detail.includes('`${flapHeldNoun(kind)} held while flapping`'), 'the episode start row names the held kind')
  assert(fs.readFileSync('src/pages/AlertDetailPage.tsx', 'utf8').includes('{flap.detail}'), 'the notice banner uses the kind-aware notice text')

  const wizard = fs.readFileSync('src/components/forms/AlertRuleWizardDialog.tsx', 'utf8')
  assert(!wizard.includes('prevents flapping alerts'), 'the hold-time hint is current')
  assert(wizard.includes('Counted from the confirmed status change'))
  assert(wizard.includes('Notify immediately even while the device is flapping'))
  assert(wizard.includes('notify_while_flapping: flapHoldApplies(s) ? s.notify_while_flapping : false') && wizard.includes("notify_while_flapping: rule.notify_while_flapping === true"), 'the flag round-trips')
  // An 'up' trigger is a recovery in the engine and is never held: no hold controls or claims for it.
  assert(/function flapHoldApplies[\s\S]*?return trigger !== 'up'/.test(wizard), 'up rules are outside the flapping hold')
  assert(wizard.includes('{flapHold && (') && wizard.includes('{flapHold && <ReviewRow label="While flapping"'))
  const policyCard = fs.readFileSync('src/components/alerts/PolicyCard.tsx', 'utf8')
  assert(policyCard.includes("rule.trigger_on !== 'up'") && policyCard.includes('<Row label="While flapping">'), 'the alert detail states the rule\'s flapping behaviour')

  for (const file of ['src/pages/DashboardPage.tsx', 'src/pages/dashboards/NetworkSecurityDashboard.tsx']) {
    const page = fs.readFileSync(file, 'utf8')
    assert(!page.includes('All devices healthy'), `${file}: the empty state comes from attentionEmptyText`)
    assert(page.includes('attentionEmptyText(summary?.flapping)') && page.includes('problemSeverity(d, a?.active || 0)') && page.includes('<FlappingTag'), file)
  }
  const avail = fs.readFileSync('src/pages/AvailabilityPage.tsx', 'utf8')
  assert(avail.includes('get({ flapping: true }).then((rows) => rows.filter((d) => d.is_flapping === true))'), 'Needs attention lists flapping devices, only those the API marks')
}

/* ═══ (c) rendered pages ═══════════════════════════════════════════════════════════════════════ */

const entry = `
export { DevicesPage } from '@/pages/DevicesPage'
export { MonitoringCard } from '@/pages/GeneralSettingsPage'
export { DeviceStatusExplanation } from '@/components/devices/DeviceStatusExplanation'
export { StatusChip, NotifyCell } from '@/components/alerts/AlertBits'
export { AlertTimeline } from '@/components/alerts/AlertTimeline'
export * as DI from '@/components/devices/deviceInventory'
export { useAuth } from '@/stores/auth'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server'
export { MemoryRouter, Routes, Route } from 'react-router-dom'
export { QueryClient, QueryClientProvider } from '@tanstack/react-query'
`
const appSrc = path.join(root, 'src')
const aliasPlugin = {
  name: 'app-alias',
  setup(b) {
    b.onResolve({ filter: /^@\// }, (args) => b.resolve(`./${args.path.slice(2)}`, { resolveDir: appSrc, kind: args.kind }))
  },
}
const bundle = await build({
  stdin: { contents: entry, resolveDir: root, loader: 'ts' },
  bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022', jsx: 'automatic',
  define: { 'process.env.NODE_ENV': '"production"' },
  plugins: [aliasPlugin],
  loader: { '.css': 'empty', '.svg': 'empty', '.png': 'empty', '.woff2': 'empty' },
  logLevel: 'silent',
})
const m = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)
const h = m.createElement
const strip = (html) => html.replace(/<[^>]*>/g, ' ').replace(/&amp;/g, '&').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, ' ').trim()
function setUser(permissions) {
  const user = { id: 'u', username: 'u', email: 'u@x', role: 'custom', permissions }
  m.useAuth.setState({ user, token: 't' })
  const initial = typeof m.useAuth.getInitialState === 'function' ? m.useAuth.getInitialState() : null
  if (initial) Object.assign(initial, { user, token: 't' })
}
const newClient = () => new m.QueryClient({ defaultOptions: { queries: { retry: false, retryOnMount: false } } })
const renderAt = (qc, url, element) =>
  m.renderToStaticMarkup(h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, { initialEntries: [url] }, h(m.Routes, null, h(m.Route, { path: '*', element })))))

// The Devices list: Flapping in the Status cell (beside SNMP: no data), the overlay KPI and its filter.
{
  const device = (id, hostname, over = {}) => ({
    id, hostname, ip_address: `10.0.0.${id.slice(1)}`, device_type: 'switch', location: null, group_id: null, group_name: null, tags: [],
    status: 'up', last_seen: iso(-1), last_rtt_ms: 1.2, ping_enabled: true, snmp_enabled: false, vendor: null, model: null, os_version: null, ...over,
  })
  const DEVICES = [
    device('d1', 'core-1', { is_flapping: true, flap_kind: 'reachability', flapping_since: iso(-38), snmp_enabled: true }),
    device('d2', 'edge-2', { status: 'down', is_flapping: true, flap_kind: 'both', flapping_since: iso(-20) }),
    device('d3', 'sw-3', { snmp_enabled: true }),
    device('d4', 'sw-4', { status: 'degraded' }),
    device('d5', 'sw-5'),
  ]
  // flapping=1 is a filter the API applies exactly (the flag is a column of the row); nothing else changes the request.
  const paramsOf = (url) => m.DI.deviceListParams(m.DI.deviceListFiltersFromParams(new URL(url, 'http://x').searchParams))
  assert.equal(paramsOf('/devices?flapping=1&status=critical').flapping, 'true')
  assert(!('status' in paramsOf('/devices?flapping=1&status=critical')), 'the status chip stays in the browser')
  assert(!('flapping' in paramsOf('/devices?flapping=yes')) && !('flapping' in paramsOf('/devices')))
  assert(!('flapping' in m.DI.deviceListFiltersFromParams(new URLSearchParams('status=critical'))), 'older bookmarks read as before')
  // `rows` is what the API answers; by default every device, as a server without the filter would.
  const page = (url, rows = DEVICES) => {
    setUser(['devices.view'])
    globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
    const qc = newClient()
    qc.setQueryData(['devices', 'list', paramsOf(url)], { data: rows, meta: { total: rows.length } })
    // Metrics answered, with nothing for the SNMP devices: "SNMP: no data".
    qc.setQueryData(['devices', 'current-metrics'], { devices: {} })
    const html = renderAt(qc, url, h(m.DevicesPage))
    delete globalThis.localStorage
    return html
  }
  // The device links of the table rows (one per device), whatever the attribute order.
  const names = (html) => [...html.matchAll(/<a [^>]*>/g)].map((x) => x[0])
    .filter((a) => /href="\/devices\/d\d"/.test(a)).map((a) => a.match(/title="([^"]+)"/)?.[1]).filter(Boolean)
  const html = page('/devices')
  const rowOf = (name) => strip(html.split('<tbody')[1].split('</tr>').find((r) => r.includes(`title="${name}"`)))
  assert.match(rowOf('core-1'), /Warning Flapping SNMP: no data/, 'the flap line does not hide SNMP: no data (and vice versa)')
  assert.match(rowOf('edge-2'), /Critical Flapping/)
  assert.doesNotMatch(rowOf('sw-5'), /Flapping/)
  assert(html.includes('Flapping (up/down changes) since'), 'the tooltip says what flaps and since when')
  const kpiButton = (doc) => [...doc.matchAll(/<button[^>]*>.*?<\/button>/gs)].map((x) => x[0]).find((b) => b.includes('title="Devices whose confirmed status keeps changing')) ?? ''
  assert.equal(strip(kpiButton(html)), 'Flapping 2', 'an overlay count: the status cards still add up to the total')
  assert(kpiButton(html).includes('aria-pressed="false"'))
  assert.match(strip(html), /Total Devices 5 Online 1 Offline 0 Warning 3 Critical 1 Flapping 2/)
  // flapping=1 filters (also against a server that ignores the parameter), and combines with Status; any other value
  // is not a filter.
  const flappingRows = DEVICES.filter((d) => d.is_flapping)
  assert.deepEqual(names(page('/devices?flapping=1', flappingRows)).sort(), ['core-1', 'edge-2'])
  assert.deepEqual(names(page('/devices?flapping=1')).sort(), ['core-1', 'edge-2'], 'a server without the filter: the browser narrows')
  const on = page('/devices?flapping=1', flappingRows)
  assert(kpiButton(on).includes('aria-pressed="true"'), 'the KPI shows it is on')
  assert.match(strip(on), /Filtered: 2 devices · cards and charts reflect the filter/, 'a scope filter: the cards count the flapping devices')
  assert.match(strip(on), /Total Devices 2 Online 0 Offline 0 Warning 1 Critical 1 Flapping 2/)
  assert(strip(on).includes('Showing flapping devices only. While they flap, notifications for the kind of change that flaps (up/down or latency/loss) are held; the other kind still notifies at once.'))
  assert(kpiButton(on).includes('title="Devices whose confirmed status keeps changing; while they flap, notifications for the kind of change that flaps (up/down or latency/loss) are held; the other kind still notifies at once. Combines with the status cards."'))
  assert(on.includes('aria-label="Clear flapping filter"'))
  assert.deepEqual(names(page('/devices?flapping=1&status=critical', flappingRows)), ['edge-2'])
  assert(strip(page('/devices?flapping=1&status=critical', flappingRows)).includes('Showing flapping devices only, with the status filter.'))
  assert.deepEqual(names(page('/devices?flapping=1&status=healthy', flappingRows)), [])
  assert.equal(names(page('/devices?flapping=yes')).length, DEVICES.length)
  assert(!page('/devices?flapping=yes').includes('data-flapping-filter'))
  assert(!page('/devices').includes('data-flapping-filter'))
}

// The explanation, full and compact, from seeded data.
{
  const render = (data, compact = false) => {
    setUser(['devices.view'])
    const qc = newClient()
    qc.setQueryData(['device-status-explanation', 'd1'], data)
    return strip(renderAt(qc, '/devices/d1', h(m.DeviceStatusExplanation, { deviceId: 'd1', compact })))
  }
  const degraded = render(explanation({ status: 'degraded', state: { ...explanation().state, hard_status: 'degraded' }, latest: { ...explanation().latest, rtt_ms: 95 } }))
  assert(degraded.includes('Why this device is degraded'))
  assert(degraded.includes('Degraded above 100 ms · recovers at or below 80 ms'), 'both limits while degraded')
  assert(degraded.includes('this check does not degrade and restarts the count of 3 consecutive checks within the recovery limits, but it counts towards recovery after 5 checks in a row'))
  assert(degraded.includes('Degraded when 3 of the last 5 checks'))
  assert(degraded.includes('Global policy'))
  for (const sentence of FORBIDDEN) assert(!degraded.includes(sentence), sentence)
  const pendingCompact = render(explanation({ state: { ...explanation().state, pending: { target: 'down', count: 2, required: 3 }, fail_streak: 2 } }), true)
  assert(pendingCompact.startsWith('Status change pending 2 of 3 consecutive failed checks; Down on the next failure'), pendingCompact.slice(0, 120))
  const flapCompact = render(explanation({ flapping: { ...explanation().flapping, active: true, kind: 'performance', since: iso(-10), perf_changes_in_window: 4 } }), true)
  assert(flapCompact.startsWith('Why this device is flapping Flapping (latency/loss); only latency/loss notifications held'), flapCompact.slice(0, 120))
  setUser(['devices.view'])
  const qc = newClient()
  qc.setQueryData(['device-status-explanation', 'd9'], explanation({ flapping: { ...explanation().flapping, active: true } }))
  assert(renderAt(qc, '/', h(m.DeviceStatusExplanation, { deviceId: 'd9', compact: true })).includes('<details') && renderAt(qc, '/', h(m.DeviceStatusExplanation, { deviceId: 'd9', compact: true })).includes(' open=""'), 'an active explanation opens itself')
  const managed = render({ status: 'up', mode: 'managed', source: 'Controller', thresholds: {}, latest: null })
  assert(managed.includes('managing controller'))
}

// The Monitoring card: sentences, Advanced, timing, presets; an older server's document still renders every field.
{
  const render = (doc) => {
    setUser(['settings.manage', 'settings.view'])
    const qc = newClient()
    qc.setQueryData(['settings', 'monitoring'], doc)
    return renderAt(qc, '/settings/general?tab=monitoring', h(m.MonitoringCard))
  }
  const html = render({ ...S.SETTINGS_DEFAULTS, effective: DEF })
  const text = strip(html)
  assert(!text.includes('Loading monitoring settings'), 'the form is filled from the cached document on the first render')
  for (const sentence of FORBIDDEN) assert(!text.includes(sentence), sentence)
  // The primary rules as sentences with inline fields, every input labelled.
  assert(text.includes('Degraded when of the last checks exceed them or get no answer.'), 'Degraded when [3] of the last [5] checks ...')
  assert(text.includes('Down after consecutive failed checks.'))
  assert(text.includes('Detect flapping on : status changes within min.'))
  const input = (label) => html.match(new RegExp(`<input[^>]*aria-label="${label.replace(/[()]/g, '\\$&')}"[^>]*>`))?.[0] ?? ''
  const valueOf = (label) => input(label).match(/value="([^"]*)"/)?.[1]
  assert.equal(valueOf('Checks over the limits for Degraded'), '3')
  assert.equal(valueOf('Checks in the Degraded window'), '5')
  assert.equal(valueOf('Consecutive failed checks for Down'), '3')
  assert.equal(valueOf('Status changes that start flapping'), '4')
  assert.equal(valueOf('Flap window in minutes'), '30')
  assert.equal(valueOf('Round-trip time above (ms)'), '100')
  assert.equal(valueOf('Packet loss above (%)'), '10')
  // Advanced is collapsed and holds the recovery limits as auto values with an override, and the remaining counts.
  assert.match(html, /<details class="group rounded-lg border border-border"><summary[^>]*>Advanced: recovery and flapping<\/summary>/, 'Advanced starts collapsed')
  assert(text.includes('Recovery latency at or below 80 ms (auto: 80% of the Degraded limit) Set manually'))
  assert(text.includes('Recovery packet loss at or below 8 % (auto: 80% of the Degraded limit) Set manually'))
  for (const label of ['Consecutive checks within the recovery limits', 'Responding checks to confirm recovery', 'Consecutive failed sensor checks for Down', 'Changes left in the window to stop flapping', 'Minutes without a change to stop flapping']) {
    assert(input(label), label)
  }
  // What the settings do: sentences, timing at 30/60/300 s, the flap interval limit and the 3-packet note.
  assert(text.includes(S.policySentences(DEF).degraded) && text.includes(S.policySentences(DEF).flapping))
  assert(text.includes('30 s 1 min–1 min 30 s within 30 s 1 min–1 min 30 s 1 min–1 min 30 s Up/down and latency/loss'))
  assert(text.includes('5 min 10–15 min within 5 min 10–15 min 10–15 min Up/down only'))
  assert(text.includes(S.flapDetectionLimitText(DEF)))
  assert(text.includes(S.lossGranularityNote(DEF)))
  assert(text.includes('Restore defaults') && text.includes('Legacy single-check preset') && text.includes('What changed:'))
  assert(!text.includes('Unsaved changes'), 'the stored document is not a change')
  assert(!text.includes('Legacy single-check behaviour'))
  // A legacy-preset document is recognised; an older server's two limits show with every other field at its default.
  assert(strip(render(S.settingsPayload(S.legacyPresetForm(S.defaultsForm())))).includes('Legacy single-check behaviour'))
  const old = render({ degraded_rtt_ms: 250, degraded_loss_pct: 20 })
  assert.match(old, /aria-label="Round-trip time above \(ms\)"[^>]*value="250"|value="250"[^>]*aria-label="Round-trip time above \(ms\)"/)
  assert(strip(old).includes('Recovery latency at or below 200 ms (auto'))
}

// Alert chips: a pending trigger held for flapping, and the notices.
{
  const r = (el) => strip(m.renderToStaticMarkup(h(m.MemoryRouter, null, el)))
  const heldRow = { status: 'pending', severity: 'critical', metadata: { flap_held: true, hold_until: iso(10) } }
  assert.equal(r(h(m.StatusChip, { alert: heldRow })), 'Held: device flapping')
  assert(r(h(m.NotifyCell, { channels: [], alert: heldRow })).startsWith('No notification channels configured Held: device flapping · until'))
  assert.equal(r(h(m.StatusChip, { alert: { status: 'pending', severity: 'critical', metadata: {} } })), 'Pending (held)')
  assert.equal(r(h(m.StatusChip, { alert: { status: 'pending', severity: 'critical', flap_held: true, hold_until: iso(10), metadata: {} } })), 'Held: device flapping')
  // A flap notice as the server classifies it (alert_workflow: notice_kind and closure reason 'flapping').
  assert(r(h(m.StatusChip, { alert: { status: 'resolved', is_notice: true, notice_kind: 'flapping', closure: { reason: 'flapping', label: 'Flapping notice' }, metadata: { flap_notice: true, flap_event: 'stop' } } })).endsWith('Notice · Flapping stopped'))
  assert(r(h(m.StatusChip, { alert: { status: 'resolved', is_notice: true, closure: { reason: 'notice', label: 'Notice' }, metadata: { flap_notice: true, flap_event: 'start' } } })).endsWith('Notice · Flapping started'))
  assert.equal(r(h(m.NotifyCell, { channels: [], delivery: null, alert: { status: 'active', metadata: {} } })), 'No notification channels configured Delivery unknown')
  // The lifecycle timeline says why a held event waits (alerts._events adds flap_held/flap_kind to its detail).
  const ev = (detail) => ({ at: iso(0), kind: 'held', label: 'Held', actor: null, source: 'evaluator', reason: null, old_status: null, new_status: 'pending', detail })
  const timeline = (events) => strip(m.renderToStaticMarkup(h(m.QueryClientProvider, { client: newClient() },
    h(m.AlertTimeline, { alert: { id: 'a1', events }, now: Date.parse(iso(1)), canComment: false }))))
  assert.match(timeline([ev({ hold_until: iso(10), flap_held: true, flap_kind: 'reachability' })]), /Device flapping \(up\/down changes\): held until .+, sent only if the status lasts/)
  assert(!timeline([ev({ hold_until: iso(10) })]).includes('flapping'), 'an ordinary hold says nothing about flapping')
}

console.log('Device status UI passed: loader parity with the contract vectors, round3, pending progress, policy sentences, timing and flap-interval limits, full-document settings payload and validation, flap badges and alert holds, activity-log episodes, explanation copy without the retired sentences, and rendered Devices list, explanation, settings card and alert chips.')
