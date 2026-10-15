/**
 * Confirmed device status, recovery limits and flapping, as the operator reads them (docs/device-status-policy.md).
 *
 * The status machine itself runs in the poller (Go) and the server (Python) and is pinned by
 * scripts/dev/device-status-contract/vectors.json; this module only presents it. It is pure (no React, no API, no
 * clock: callers pass `nowMs` and, where a time is printed, a formatter) so scripts/test-device-status-flapping.mjs can
 * unit-test it, and it is the single source of the wording used by the device list and detail pages, the status
 * explanation, the Monitoring settings card and the alert surfaces.
 */

/* ── Policy ──────────────────────────────────────────────────────────────── */

/** The effective policy (the server's `effective` object and the explanation's `policy` block). */
export interface MonitoringPolicy {
  degraded_rtt_ms: number
  degraded_loss_pct: number
  degraded_rtt_clear_ms: number
  degraded_loss_clear_pct: number
  degraded_confirm_checks: number
  degraded_window_checks: number
  degraded_clear_checks: number
  down_confirm_checks: number
  sensor_down_confirm_checks: number
  up_confirm_checks: number
  flap_detection_enabled: boolean
  flap_window_minutes: number
  flap_start_changes: number
  flap_stop_changes: number
  flap_stable_minutes: number
}

/** The stored 'monitoring' document: the policy, except that a recovery limit may be null ("derive it"). */
export type MonitoringSettingsDoc = Omit<MonitoringPolicy, 'degraded_rtt_clear_ms' | 'degraded_loss_clear_pct'> & {
  degraded_rtt_clear_ms: number | null
  degraded_loss_clear_pct: number | null
}

/** Every key of the document, in the order the server's DEFAULTS list them. A PUT always sends all of them. */
export const POLICY_KEYS = [
  'degraded_rtt_ms', 'degraded_loss_pct', 'degraded_rtt_clear_ms', 'degraded_loss_clear_pct',
  'degraded_confirm_checks', 'degraded_window_checks', 'degraded_clear_checks',
  'down_confirm_checks', 'sensor_down_confirm_checks', 'up_confirm_checks',
  'flap_detection_enabled', 'flap_window_minutes', 'flap_start_changes', 'flap_stop_changes', 'flap_stable_minutes',
] as const satisfies ReadonlyArray<keyof MonitoringPolicy>

export const SETTINGS_DEFAULTS: MonitoringSettingsDoc = {
  degraded_rtt_ms: 100,
  degraded_loss_pct: 10,
  degraded_rtt_clear_ms: null,
  degraded_loss_clear_pct: null,
  degraded_confirm_checks: 3,
  degraded_window_checks: 5,
  degraded_clear_checks: 3,
  down_confirm_checks: 3,
  sensor_down_confirm_checks: 1,
  up_confirm_checks: 1,
  flap_detection_enabled: true,
  flap_window_minutes: 30,
  flap_start_changes: 4,
  flap_stop_changes: 1,
  flap_stable_minutes: 10,
}

type IntKey =
  | 'degraded_confirm_checks' | 'degraded_window_checks' | 'degraded_clear_checks' | 'down_confirm_checks'
  | 'sensor_down_confirm_checks' | 'up_confirm_checks' | 'flap_window_minutes' | 'flap_start_changes'
  | 'flap_stop_changes' | 'flap_stable_minutes'

/** Integer fields: [minimum, maximum] before the cross-field rules (device_status_machine.INT_RANGES). */
export const INT_RANGES: Record<IntKey, readonly [number, number]> = {
  degraded_confirm_checks: [1, 20],
  degraded_window_checks: [1, 20],
  degraded_clear_checks: [1, 20],
  down_confirm_checks: [1, 10],
  sensor_down_confirm_checks: [1, 10],
  up_confirm_checks: [1, 10],
  flap_window_minutes: [5, 1440],
  flap_start_changes: [3, 64],
  flap_stop_changes: [0, 63],
  flap_stable_minutes: [1, 1440],
}
export const RTT_MAX_MS = 10000
export const LOSS_MIN_PCT = 1
export const LOSS_MAX_PCT = 100
/** Packets in one central ping check: loss per check is 0, 33.3, 66.7 or 100%. */
export const PACKETS_PER_CHECK = 3

/**
 * Round to 3 decimals exactly as the server does (Python `round(x, 3)`, Go `FormatFloat(x, 'f', 3)`): the correctly
 * rounded decimal of the exact binary value, ties to even. `toFixed` also rounds the exact value, so only an exact
 * tie differs (it takes the larger neighbour); scaling by 1000 first would add its own error (0.8 x 1.005625 is
 * 0.80449..., 0.804, not 0.805).
 */
export function round3(x: number): number {
  if (!Number.isFinite(x)) return x
  const sign = x < 0 ? -1 : 1
  const a = Math.abs(x)
  if (a < 1e21) {
    // 100 fraction digits hold the exact expansion of every double above 2^-48; anything smaller rounds to 0.
    const exact = a.toFixed(100)
    const dot = exact.indexOf('.')
    const kept = exact.slice(0, dot + 4)
    if (/^50*$/.test(exact.slice(dot + 4)) && Number(kept[kept.length - 1]) % 2 === 0) return sign * Number(kept)
  }
  return sign * Number(a.toFixed(3))
}

/** Up to 3 decimals, trailing zeros and dot trimmed (the server's fmt3): 500, 5.6, 1234.568. */
export function fmt3(x: number): string {
  return round3(x).toFixed(3).replace(/\.?0+$/, '')
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
/** A finite JSON number (booleans and strings excluded); anything else means "use the default". */
const jsonNumber = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

function intField(raw: Record<string, unknown>, key: IntKey): number {
  const [lo, hi] = INT_RANGES[key]
  const v = jsonNumber(raw[key])
  if (v === null || !Number.isInteger(v)) return SETTINGS_DEFAULTS[key]
  return Math.min(Math.max(v, lo), hi)
}

/**
 * The effective policy for a stored document: device_status_machine.policy_from_settings (and Go
 * PolicyFromSettings), so previews in the browser say what the poller will do. Every field is parsed on its own; a
 * missing, null, non-numeric or non-finite value takes the default; integer fields accept only integral numbers;
 * values clamp to their range; then rtt_clear <= rtt, loss_clear <= loss, window >= confirm, stop <= start - 1 and
 * stable <= flap window. A latency limit <= 0 is the default and a recovery latency limit <= 0 is derived.
 */
export function effectivePolicy(doc: unknown): MonitoringPolicy {
  const raw = isRecord(doc) ? doc : {}
  const rttIn = jsonNumber(raw.degraded_rtt_ms)
  const rtt = rttIn === null || rttIn <= 0 ? SETTINGS_DEFAULTS.degraded_rtt_ms : Math.min(rttIn, RTT_MAX_MS)
  const lossIn = jsonNumber(raw.degraded_loss_pct)
  const loss = lossIn === null ? SETTINGS_DEFAULTS.degraded_loss_pct : Math.min(Math.max(lossIn, LOSS_MIN_PCT), LOSS_MAX_PCT)
  const rttClearIn = jsonNumber(raw.degraded_rtt_clear_ms)
  const rttClear = Math.min(rttClearIn === null || rttClearIn <= 0 ? round3(0.8 * rtt) : Math.min(rttClearIn, RTT_MAX_MS), rtt)
  const lossClearIn = jsonNumber(raw.degraded_loss_clear_pct)
  const lossClear = Math.min(lossClearIn === null ? round3(0.8 * loss) : Math.min(Math.max(lossClearIn, 0), LOSS_MAX_PCT), loss)
  const confirm = intField(raw, 'degraded_confirm_checks')
  const flapWindow = intField(raw, 'flap_window_minutes')
  const start = intField(raw, 'flap_start_changes')
  return {
    degraded_rtt_ms: rtt,
    degraded_loss_pct: loss,
    degraded_rtt_clear_ms: rttClear,
    degraded_loss_clear_pct: lossClear,
    degraded_confirm_checks: confirm,
    degraded_window_checks: Math.max(intField(raw, 'degraded_window_checks'), confirm),
    degraded_clear_checks: intField(raw, 'degraded_clear_checks'),
    down_confirm_checks: intField(raw, 'down_confirm_checks'),
    sensor_down_confirm_checks: intField(raw, 'sensor_down_confirm_checks'),
    up_confirm_checks: intField(raw, 'up_confirm_checks'),
    flap_detection_enabled: typeof raw.flap_detection_enabled === 'boolean' ? raw.flap_detection_enabled : SETTINGS_DEFAULTS.flap_detection_enabled,
    flap_window_minutes: flapWindow,
    flap_start_changes: start,
    flap_stop_changes: Math.min(intField(raw, 'flap_stop_changes'), start - 1),
    flap_stable_minutes: Math.min(intField(raw, 'flap_stable_minutes'), flapWindow),
  }
}

/** Whether the policy is the documented single-check (legacy) preset. */
export function isLegacyPolicy(p: MonitoringPolicy): boolean {
  return p.degraded_confirm_checks === 1 && p.degraded_window_checks === 1 && p.degraded_clear_checks === 1
    && !p.flap_detection_enabled && p.degraded_rtt_clear_ms === p.degraded_rtt_ms && p.degraded_loss_clear_pct === p.degraded_loss_pct
}

/* ── Canonical predicates (classification, recovery and wording use the same ones) ─────────────────────────────── */

export const exceedsMs = (value: number, limit: number): boolean => value - limit > 1e-6
/** Packet loss in percent (the explanation's `packet_loss_pct` = ratio x 100) against a percent limit. */
export const exceedsPct = (lossPct: number, limitPct: number): boolean => lossPct - limitPct > 1e-4

export type CheckClass = 'fail' | 'breach' | 'marginal' | 'clear'

/** One check against the policy: breach (over a Degraded limit), clear (within both recovery limits) or between. */
export function classifyCheck(sample: { is_up: boolean; rtt_ms: number; packet_loss_pct: number }, p: MonitoringPolicy): CheckClass {
  if (!sample.is_up) return 'fail'
  const rtt = Number(sample.rtt_ms) || 0
  const loss = Number(sample.packet_loss_pct) || 0
  if (exceedsMs(rtt, p.degraded_rtt_ms) || exceedsPct(loss, p.degraded_loss_pct)) return 'breach'
  if (!exceedsMs(rtt, p.degraded_rtt_clear_ms) && !exceedsPct(loss, p.degraded_loss_clear_pct)) return 'clear'
  return 'marginal'
}

/* ── Durations and times ─────────────────────────────────────────────────── */

/** An API timestamp as epoch ms; a value without an offset is UTC (as lib/utils relativeTime reads it). */
export function parseApiTime(value: string | null | undefined): number | null {
  if (!value) return null
  const s = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value.replace(' ', 'T')}Z`
  const ms = Date.parse(s)
  return Number.isFinite(ms) ? ms : null
}

/** Compact elapsed time: 45s, 12m, 2h 14m, 3d 4h. */
export function formatSpan(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return ''
  const s = Math.floor(seconds)
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return h ? `${d}d ${h}h` : `${d}d`
  if (h > 0) return m ? `${h}h ${m}m` : `${h}h`
  if (m > 0) return `${m}m`
  return `${s}s`
}

/** A configured duration in words: 30 s, 1 min 30 s, 5 min, 1 h 15 min. */
export function fmtSeconds(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} s`
  if (s < 3600) {
    const m = Math.floor(s / 60)
    const r = s % 60
    return r ? `${m} min ${r} s` : `${m} min`
  }
  const h = Math.floor(s / 3600)
  const m = Math.round((s % 3600) / 60)
  return m ? `${h} h ${m} min` : `${h} h`
}

/** "for 2h 14m" since the last confirmed status change; null when the change time is unknown. */
export function statusForText(statusChangedAt: string | null | undefined, nowMs: number): string | null {
  const at = parseApiTime(statusChangedAt)
  if (at === null) return null
  return `for ${formatSpan(Math.max(0, (nowMs - at) / 1000))}`
}

export type TimeFormatter = (epochMs: number) => string
const defaultTime: TimeFormatter = (ms) => new Date(ms).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })

const plural = (n: number, word: string, many = `${word}s`) => `${n} ${n === 1 ? word : many}`

/* ── Flapping ────────────────────────────────────────────────────────────── */

export type FlapKind = 'reachability' | 'performance' | 'both'

/** The flap fields GET /devices and GET /devices/{id} add (all optional: older servers omit them). */
export interface DeviceFlapFields {
  is_flapping?: boolean | null
  flapping_since?: string | null
  flap_kind?: string | null
  status_changed_at?: string | null
}

export function isFlapping(d: DeviceFlapFields | null | undefined): boolean {
  return d?.is_flapping === true
}

export function flapKindOf(kind: unknown): FlapKind | null {
  return kind === 'reachability' || kind === 'performance' || kind === 'both' ? kind : null
}

/** What changes: reachability is into/out of Down, performance is Up <-> Degraded. */
export const FLAP_KIND_LABEL: Record<FlapKind, string> = {
  reachability: 'up/down',
  performance: 'latency/loss',
  both: 'up/down and latency/loss',
}

/*
 * What a flap hold covers. The alert engine holds a status trigger only while the class of ITS OWN change flaps
 * (alert_engine.evaluate_status_change: flap_held = event.flapping, the transition's class flag): a device flapping on
 * latency/loss still pages a Down at once, one flapping up/down still pages Up -> Degraded at once
 * (docs/device-status-policy.md: "latency jitter can never hold back a Down notification"). Recoveries are never held;
 * they follow their trigger. So the copy names the kind of change whose notifications wait, never "all of them".
 */

const lastsText = (stableMinutes: unknown): string => (typeof stableMinutes === 'number'
  ? `: a status that lasts ${stableMinutes} min still notifies`
  : ' until a status lasts the flap stability time')

/** The hold of one flapping device, as sentences: which changes wait, how long, and which still notify at once. */
export function flapHeldText(kind: unknown, stableMinutes?: number | null): string {
  const lasts = lastsText(stableMinutes)
  switch (flapKindOf(kind)) {
    case 'reachability': return `Notifications for changes into or out of Down are held${lasts}. Changes between Up and Degraded still notify at once.`
    case 'performance': return `Notifications for changes between Up and Degraded are held${lasts}. Changes into or out of Down still notify at once.`
    case 'both': return `Status notifications for this device are held${lasts}.`
    default: return `Notifications for the kind of change that is flapping are held${lasts}.`
  }
}

/** The notifications a flap hold covers, as a short noun phrase: "up/down notifications". */
export function flapHeldNoun(kind: unknown): string {
  const k = flapKindOf(kind)
  return k === 'both' ? 'status notifications' : k ? `${FLAP_KIND_LABEL[k]} notifications` : 'notifications for the flapping changes'
}

/** The hold where the kind is not known per device (fleet counts, filters, settings): only the flapping kind waits. */
export const FLAP_HOLD_FLEET_TEXT = 'notifications for the kind of change that flaps (up/down or latency/loss) are held; the other kind still notifies at once'

const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

/** Badge text: "Flapping", or "Flapping · up/down" where there is room for the kind. */
export function flapBadgeText(kind: unknown, withKind = false): string {
  const k = flapKindOf(kind)
  return withKind && k ? `Flapping · ${FLAP_KIND_LABEL[k]}` : 'Flapping'
}

export interface FlapTooltipInput {
  kind?: string | null
  since?: string | null
  /** Changes of the flapping class(es) in the window, when known (the explanation's counters). */
  changes?: number | null
  windowMinutes?: number | null
  stableMinutes?: number | null
  earliestStopAt?: string | null
}

/**
 * The tooltip of a Flapping badge: what flaps, since when, how many changes, when it can end at the earliest, and
 * which notifications are held (only the flapping kind; a status that lasts the stability time still notifies).
 */
export function flapTooltip(info: FlapTooltipInput, nowMs: number, fmtTime: TimeFormatter = defaultTime): string {
  const kind = flapKindOf(info.kind)
  const parts: string[] = []
  const since = parseApiTime(info.since)
  parts.push(`Flapping${kind ? ` (${FLAP_KIND_LABEL[kind]} changes)` : ''}${since !== null ? ` since ${fmtTime(since)} (${formatSpan(Math.max(0, (nowMs - since) / 1000))})` : ''}.`)
  if (typeof info.changes === 'number' && typeof info.windowMinutes === 'number') {
    parts.push(`${plural(info.changes, 'status change')} in the last ${info.windowMinutes} min.`)
  }
  parts.push(flapHeldText(kind, info.stableMinutes))
  const earliest = parseApiTime(info.earliestStopAt)
  if (earliest !== null) parts.push(`Ends ${fmtTime(earliest)} at the earliest if the status stops changing.`)
  return parts.join(' ')
}

/** Fleet ranking for "Needs attention": down 3, degraded or flapping 2, open alerts 1, else 0 (not listed). */
export function problemSeverity(d: { status: string } & DeviceFlapFields, activeAlerts: number): number {
  if (d.status === 'down') return 3
  if (d.status === 'degraded' || isFlapping(d)) return 2
  return activeAlerts > 0 ? 1 : 0
}

/**
 * The empty state of a "Needs attention" list: "All devices healthy" only when no device is flapping either (a
 * flapping device may be beyond the loaded rows; the summary counts all of them).
 */
export function attentionEmptyText(summaryFlapping: number | null | undefined): string {
  const n = typeof summaryFlapping === 'number' && summaryFlapping > 0 ? summaryFlapping : 0
  return n > 0 ? `${plural(n, 'device')} flapping (${flapHeldNoun(null)} held)` : 'All devices healthy 🎉'
}

/* ── Pending (soft) changes ──────────────────────────────────────────────── */

export interface PendingChange { target: string; count: number; required: number }

/**
 * Progress towards a confirmed change, from the explanation's `state.pending` (device_status_machine.pending): Down
 * (consecutive failed checks), Degraded (bad checks in the window), Up from Down (responding checks) or Up from
 * Degraded (consecutive checks within the recovery limits). `count` may reach `required` after a policy decrease.
 */
export function pendingText(p: PendingChange | null | undefined, ctx: { hardStatus?: string | null; windowSize?: number | null } = {}): string | null {
  if (!p || typeof p.count !== 'number' || typeof p.required !== 'number' || p.count <= 0) return null
  const { count, required } = p
  if (p.target === 'down') {
    if (count >= required) return `${plural(count, 'consecutive failed check')} (Down needs ${required}); Down is confirmed on the next failed check`
    if (count === required - 1) return `${count} of ${required} consecutive failed checks; Down on the next failure`
    return `${count} of ${required} consecutive failed checks towards Down`
  }
  if (p.target === 'degraded') {
    const win = typeof ctx.windowSize === 'number' && ctx.windowSize > 0 ? ` of the last ${ctx.windowSize}` : ''
    return `${plural(count, 'check')}${win} slow, lossy or unanswered; Degraded at ${required}`
  }
  if (p.target === 'up' && ctx.hardStatus === 'down') {
    return `Responding for ${count} of ${required} checks needed to confirm recovery`
  }
  if (p.target === 'up') {
    return `${count} of ${required} consecutive checks within the recovery limits; Up at ${required}`
  }
  return null
}

/** The device header chip: pending Down only (pending Degraded is too common to deserve a header chip). */
export function pendingDownChip(p: PendingChange | null | undefined, hardStatus: string | null | undefined): string | null {
  if (!p || p.target !== 'down' || hardStatus === 'down' || hardStatus === 'maintenance' || !(p.count > 0)) return null
  return `Down pending · ${p.count} of ${p.required} failed checks`
}

/* ── Policy in words ─────────────────────────────────────────────────────── */

export interface PolicySentences {
  degraded: string
  recovery: string
  down: string
  flapping: string
}

const checksWord = (n: number) => plural(n, 'check')

/** The rules of a policy as sentences (explanation and settings preview). `downConfirm` is the device owner's. */
export function policySentences(p: MonitoringPolicy, opts: { downConfirm?: number; packets?: number | null } = {}): PolicySentences {
  const rtt = `${fmt3(p.degraded_rtt_ms)} ms`
  const loss = `${fmt3(p.degraded_loss_pct)}%`
  const down = opts.downConfirm ?? p.down_confirm_checks
  const single = p.degraded_confirm_checks === 1 && p.degraded_window_checks === 1
  const degraded = single
    ? `Degraded when a check's latency is above ${rtt} or its packet loss above ${loss} (equal values are within the limit).`
    : `Degraded when ${p.degraded_confirm_checks} of the last ${p.degraded_window_checks} checks have latency above ${rtt}, packet loss above ${loss} or no response (equal values are within the limit).`
  const recovery = recoverySentence(p)
  const packets = typeof opts.packets === 'number' && opts.packets > 0 ? ` (${opts.packets} packets each)` : ''
  const up = p.up_confirm_checks === 1 ? 'the first responding check' : `${p.up_confirm_checks} consecutive responding checks`
  const then = p.degraded_confirm_checks > 1
    ? 'A recovering device returns to Up first; Degraded is then confirmed on its own.'
    : 'It is Degraded straight away when that check is over the limits.'
  const downText = `Down after ${plural(down, 'consecutive failed check')}${packets}; back to Up after ${up}. ${then} Unanswered checks also count towards Degraded.`
  const stop = p.flap_stop_changes === 0 ? 'no change remains' : `at most ${plural(p.flap_stop_changes, 'change')} remain${p.flap_stop_changes === 1 ? 's' : ''}`
  const flapping = p.flap_detection_enabled
    ? `Flapping when the status changes ${p.flap_start_changes} times within ${p.flap_window_minutes} min (up/down and latency/loss changes are counted separately). It ends when ${stop} in the window or after ${p.flap_stable_minutes} min without a change. While up/down or latency/loss changes flap, notifications for that kind of change are held: a status that lasts ${p.flap_stable_minutes} min still notifies. The other kind still notifies at once.`
    : 'Flap detection is off: every confirmed status change notifies as usual.'
  return { degraded, recovery, down: downText, flapping }
}

/** Whether the recovery limits equal the Degraded limits: every check that is not slow, lossy or unanswered is clear. */
const noHysteresis = (p: MonitoringPolicy) => p.degraded_rtt_clear_ms === p.degraded_rtt_ms && p.degraded_loss_clear_pct === p.degraded_loss_pct

/**
 * Degraded -> Up as the machine decides it: degraded_clear_checks consecutive checks within the recovery limits, OR a
 * full window (degraded_window_checks since the last change) without a slow, lossy or unanswered check. A check in
 * the band between the two limits restarts the first path but counts towards the second, so only the paths that can
 * actually decide are named: without a band both paths need the same checks (the faster count wins); with a band
 * and a window no longer than the recovery count, the window always recovers first and the recovery limits never
 * decide.
 */
function recoverySentence(p: MonitoringPolicy): string {
  const clear = p.degraded_clear_checks
  const win = p.degraded_window_checks
  if (noHysteresis(p)) {
    const n = Math.min(clear, win)
    return `Back to Up from Degraded after ${n === 1 ? 'one check' : plural(n, 'consecutive check')} at or below the Degraded limits.`
  }
  const limits = `(${fmt3(p.degraded_rtt_clear_ms)} ms, ${fmt3(p.degraded_loss_clear_pct)}%)`
  if (win <= clear) {
    const within = `(latency at or below ${fmt3(p.degraded_rtt_ms)} ms, packet loss at or below ${fmt3(p.degraded_loss_pct)}%)`
    const after = win === 1 ? `one check that is not slow, lossy or unanswered ${within}` : `${win} checks in a row with no slow, lossy or unanswered check ${within}`
    return `Back to Up from Degraded after ${after}. The recovery limits ${limits} have no effect: a window of ${plural(win, 'check')} is no longer than ${plural(clear, 'consecutive recovery check')}.`
  }
  const streak = clear === 1 ? 'one check' : `${clear} consecutive checks`
  return `Back to Up from Degraded after ${streak} at or below the recovery limits ${limits}, or after ${plural(win, 'check')} in a row with no slow, lossy or unanswered check.`
}

/**
 * Minimum number of check intervals between the first and the last of `changes` confirmed changes, when the gaps
 * alternate between `a` and `b` checks (into/out of Down: down and up confirmation; into/out of Degraded: degraded
 * confirmation and the faster recovery path).
 */
export function flapSpanChecks(changes: number, a: number, b: number): number {
  const gaps = Math.max(0, changes - 1)
  return Math.ceil(gaps / 2) * Math.min(a, b) + Math.floor(gaps / 2) * Math.max(a, b)
}

/** Largest whole-second interval whose span still fits strictly inside the window (prune keeps c > ts - window). */
function largestInterval(windowMinutes: number, spanChecks: number): number {
  if (spanChecks <= 0) return Infinity
  return Math.ceil((windowMinutes * 60) / spanChecks) - 1
}

/**
 * The longest ping interval (seconds) at which flapping can still start: flap_start_changes changes must fall
 * within flap_window_minutes, and each change needs its confirmation checks. Up/down changes alternate between
 * down and up confirmation; latency/loss changes between degraded confirmation and the faster recovery path
 * (clear checks, or a clean window).
 */
export function maxDetectableIntervals(p: MonitoringPolicy): { reachability: number; performance: number; sensorReachability: number } {
  const k = p.flap_start_changes
  const recover = Math.min(p.degraded_clear_checks, p.degraded_window_checks)
  return {
    reachability: largestInterval(p.flap_window_minutes, flapSpanChecks(k, p.down_confirm_checks, p.up_confirm_checks)),
    performance: largestInterval(p.flap_window_minutes, flapSpanChecks(k, p.degraded_confirm_checks, recover)),
    sensorReachability: largestInterval(p.flap_window_minutes, flapSpanChecks(k, p.sensor_down_confirm_checks, p.up_confirm_checks)),
  }
}

/** "20–30 s", "2–3 min", "1 min–1 min 30 s": one unit when both ends share it. */
function secondsRange(lo: number, hi: number): string {
  if (hi < 60) return `${lo}–${hi} s`
  if (lo % 60 === 0 && hi % 60 === 0 && hi < 3600) return `${lo / 60}–${hi / 60} min`
  return `${fmtSeconds(lo)}–${fmtSeconds(hi)}`
}

/** How long n confirming checks take after a condition begins: (n-1) to n intervals (the first check may be one away). */
export function confirmationDelay(checks: number, intervalSeconds: number): string {
  if (checks <= 1) return `within ${fmtSeconds(intervalSeconds)}`
  return secondsRange((checks - 1) * intervalSeconds, checks * intervalSeconds)
}

export interface TimingRow {
  intervalSeconds: number
  down: string
  up: string
  degraded: string
  degradedClear: string
  /** Which flapping can start at this interval on the central poller: 'both', 'reachability', 'performance' or 'none'. */
  flap: 'both' | 'reachability' | 'performance' | 'none'
}

/** How long each confirmation takes at the given intervals (the settings card shows 30, 60 and 300 s). */
export function timingRows(p: MonitoringPolicy, intervals: readonly number[] = [30, 60, 300]): TimingRow[] {
  const max = maxDetectableIntervals(p)
  return intervals.map((i) => {
    const reach = p.flap_detection_enabled && i <= max.reachability
    const perf = p.flap_detection_enabled && i <= max.performance
    return {
      intervalSeconds: i,
      down: confirmationDelay(p.down_confirm_checks, i),
      up: confirmationDelay(p.up_confirm_checks, i),
      degraded: confirmationDelay(p.degraded_confirm_checks, i),
      // The faster recovery path, as in maxDetectableIntervals: clear checks, or a full clean window.
      degradedClear: confirmationDelay(Math.min(p.degraded_clear_checks, p.degraded_window_checks), i),
      flap: reach && perf ? 'both' : reach ? 'reachability' : perf ? 'performance' : 'none',
    }
  })
}

export const TIMING_FLAP_LABEL: Record<TimingRow['flap'], string> = {
  both: 'Up/down and latency/loss',
  reachability: 'Up/down only',
  performance: 'Latency/loss only',
  none: 'Cannot start',
}

/** The interval limit of flap detection (settings card and explanation). */
export function flapDetectionLimitText(p: MonitoringPolicy): string {
  if (!p.flap_detection_enabled) return 'Flap detection is off.'
  const m = maxDetectableIntervals(p)
  return `Flapping can start only on devices polled at intervals of up to ${fmtSeconds(m.reachability)} for up/down changes (${fmtSeconds(m.sensorReachability)} on remote sensors) and up to ${fmtSeconds(m.performance)} for latency/loss changes; slower devices cannot reach ${p.flap_start_changes} confirmed changes within ${p.flap_window_minutes} min.`
}

/** Packets that must be lost in one check for its loss to exceed `limitPct` (PACKETS_PER_CHECK + 1: never). */
export function packetsLostToExceed(limitPct: number): number {
  for (let k = 1; k <= PACKETS_PER_CHECK; k++) if (exceedsPct((k * 100) / PACKETS_PER_CHECK, limitPct)) return k
  return PACKETS_PER_CHECK + 1
}

/** The 3-packet granularity note: one lost packet is already 33.3% loss. */
export function lossGranularityNote(p: MonitoringPolicy): string {
  const lost = packetsLostToExceed(p.degraded_loss_pct)
  const lossy = lost > PACKETS_PER_CHECK
    ? `With the ${fmt3(p.degraded_loss_pct)}% limit, packet loss never makes a responding check lossy.`
    : `With the ${fmt3(p.degraded_loss_pct)}% limit, a check is lossy when ${lost === 1 ? 'one packet is' : `${lost} of ${PACKETS_PER_CHECK} packets are`} lost.`
  const clear = packetsLostToExceed(p.degraded_loss_clear_pct) === 1
    ? ` A recovery limit below 33.3% means a check counts towards the consecutive recovery checks only with no loss at all.`
    : ''
  return `Each check sends ${PACKETS_PER_CHECK} packets, so its loss is 0%, 33.3%, 66.7% or 100%. ${lossy}${clear}`
}

/* ── Monitoring settings form ────────────────────────────────────────────── */

/** The form keeps what the user typed (strings) so a half-typed number is not rewritten under the cursor. */
export interface MonitoringForm {
  degraded_rtt_ms: string
  degraded_loss_pct: string
  /** false: the recovery limit is derived (sent as null, 80% of the Degraded limit). */
  rtt_clear_override: boolean
  degraded_rtt_clear_ms: string
  loss_clear_override: boolean
  degraded_loss_clear_pct: string
  degraded_confirm_checks: string
  degraded_window_checks: string
  degraded_clear_checks: string
  down_confirm_checks: string
  sensor_down_confirm_checks: string
  up_confirm_checks: string
  flap_detection_enabled: boolean
  flap_window_minutes: string
  flap_start_changes: string
  flap_stop_changes: string
  flap_stable_minutes: string
}

export type MonitoringFormField = keyof MonitoringForm

const NUMERIC_FIELDS = [
  'degraded_rtt_ms', 'degraded_loss_pct', 'degraded_confirm_checks', 'degraded_window_checks', 'degraded_clear_checks',
  'down_confirm_checks', 'sensor_down_confirm_checks', 'up_confirm_checks', 'flap_window_minutes', 'flap_start_changes',
  'flap_stop_changes', 'flap_stable_minutes',
] as const

const asText = (v: unknown, fallback: number | null): string => {
  if (typeof v === 'number' && Number.isFinite(v)) return String(v)
  return fallback === null ? '' : String(fallback)
}

/** The form for GET /settings/monitoring (stored values over defaults); fields an older server omits take the defaults. */
export function formFromSettings(doc: unknown): MonitoringForm {
  const raw = isRecord(doc) ? doc : {}
  const form = {} as MonitoringForm
  for (const key of NUMERIC_FIELDS) form[key] = asText(raw[key], SETTINGS_DEFAULTS[key])
  const rttClear = jsonNumber(raw.degraded_rtt_clear_ms)
  const lossClear = jsonNumber(raw.degraded_loss_clear_pct)
  form.rtt_clear_override = rttClear !== null
  form.degraded_rtt_clear_ms = rttClear === null ? '' : String(rttClear)
  form.loss_clear_override = lossClear !== null
  form.degraded_loss_clear_pct = lossClear === null ? '' : String(lossClear)
  form.flap_detection_enabled = typeof raw.flap_detection_enabled === 'boolean' ? raw.flap_detection_enabled : SETTINGS_DEFAULTS.flap_detection_enabled
  return form
}

export const defaultsForm = (): MonitoringForm => formFromSettings(SETTINGS_DEFAULTS)

const parseField = (s: string): number => (s.trim() === '' ? NaN : Number(s.trim()))

/**
 * The full document for PUT /settings/monitoring: ALL keys, every time, so nothing the form shows is left to the
 * stored value or a default. A derived recovery limit is sent as null.
 */
export function settingsPayload(form: MonitoringForm): MonitoringSettingsDoc {
  const out = {} as Record<string, number | boolean | null>
  for (const key of NUMERIC_FIELDS) out[key] = parseField(form[key])
  out.degraded_rtt_clear_ms = form.rtt_clear_override ? parseField(form.degraded_rtt_clear_ms) : null
  out.degraded_loss_clear_pct = form.loss_clear_override ? parseField(form.degraded_loss_clear_pct) : null
  out.flap_detection_enabled = form.flap_detection_enabled
  const ordered = {} as Record<string, number | boolean | null>
  for (const key of POLICY_KEYS) ordered[key] = out[key]
  return ordered as unknown as MonitoringSettingsDoc
}

/** The policy the form would produce (the server's loader applied to the payload), for live previews. */
export function policyFromForm(form: MonitoringForm): MonitoringPolicy {
  const doc = settingsPayload(form) as unknown as Record<string, unknown>
  const clean: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(doc)) if (!(typeof v === 'number' && Number.isNaN(v))) clean[k] = v
  return effectivePolicy(clean)
}

/**
 * Client validation mirroring the server's 422 rules for the merged document (the PUT sends all of it):
 * latency (0, 10000] ms, loss [1, 100]%, recovery limits (0, latency] and [0, loss], integer counts in their ranges,
 * window >= confirmation, stop changes below start changes, stable minutes within the flap window.
 */
export function validateMonitoringForm(form: MonitoringForm): Partial<Record<MonitoringFormField, string>> {
  const errors: Partial<Record<MonitoringFormField, string>> = {}
  const v = (key: (typeof NUMERIC_FIELDS)[number]) => parseField(form[key])
  const rtt = v('degraded_rtt_ms')
  if (!Number.isFinite(rtt) || rtt <= 0 || rtt > RTT_MAX_MS) errors.degraded_rtt_ms = 'Enter a latency above 0 and up to 10,000 ms.'
  const loss = v('degraded_loss_pct')
  if (!Number.isFinite(loss) || loss < LOSS_MIN_PCT || loss > LOSS_MAX_PCT) errors.degraded_loss_pct = 'Enter a packet loss from 1 to 100%.'
  if (form.rtt_clear_override) {
    const c = parseField(form.degraded_rtt_clear_ms)
    if (!Number.isFinite(c) || c <= 0) errors.degraded_rtt_clear_ms = 'Enter a recovery latency above 0 ms.'
    else if (Number.isFinite(rtt) && c > rtt) errors.degraded_rtt_clear_ms = 'The recovery latency cannot be above the Degraded limit.'
  }
  if (form.loss_clear_override) {
    const c = parseField(form.degraded_loss_clear_pct)
    if (!Number.isFinite(c) || c < 0) errors.degraded_loss_clear_pct = 'Enter a recovery packet loss of 0% or more.'
    else if (Number.isFinite(loss) && c > loss) errors.degraded_loss_clear_pct = 'The recovery packet loss cannot be above the Degraded limit.'
  }
  const int = (key: IntKey, label: string): number => {
    const n = v(key)
    const [lo, hi] = INT_RANGES[key]
    if (!Number.isInteger(n) || n < lo || n > hi) {
      errors[key] = `${label} must be a whole number from ${lo} to ${hi}.`
      return NaN
    }
    return n
  }
  const confirm = int('degraded_confirm_checks', 'Checks over the limits')
  const window = int('degraded_window_checks', 'The check window')
  if (Number.isFinite(confirm) && Number.isFinite(window) && window < confirm) {
    errors.degraded_window_checks = 'The window must hold at least as many checks as Degraded needs.'
  }
  int('degraded_clear_checks', 'Recovery checks')
  int('down_confirm_checks', 'Failed checks for Down')
  int('sensor_down_confirm_checks', 'Failed sensor checks for Down')
  int('up_confirm_checks', 'Responding checks for Up')
  const flapWindow = int('flap_window_minutes', 'The flap window')
  const start = int('flap_start_changes', 'Changes to start flapping')
  const stop = int('flap_stop_changes', 'Changes to stop flapping')
  if (Number.isFinite(start) && Number.isFinite(stop) && stop > start - 1) {
    errors.flap_stop_changes = 'Flapping must stop below the number of changes that starts it.'
  }
  const stable = int('flap_stable_minutes', 'Minutes without a change')
  if (Number.isFinite(stable) && Number.isFinite(flapWindow) && stable > flapWindow) {
    errors.flap_stable_minutes = 'The stability time cannot be longer than the flap window.'
  }
  return errors
}

/**
 * The documented single-check preset (device_status_machine.legacy_preset): one check confirms and clears Degraded,
 * the recovery limits equal the Degraded limits (no hysteresis), flap detection is off. Down/Up counts are kept.
 */
export function legacyPresetForm(form: MonitoringForm): MonitoringForm {
  const base = policyFromForm(form)
  return {
    ...form,
    degraded_confirm_checks: '1',
    degraded_window_checks: '1',
    degraded_clear_checks: '1',
    rtt_clear_override: true,
    degraded_rtt_clear_ms: String(base.degraded_rtt_ms),
    loss_clear_override: true,
    degraded_loss_clear_pct: String(base.degraded_loss_pct),
    flap_detection_enabled: false,
  }
}

/* ── Status explanation (GET /devices/{id}/status-explanation) ─────────────────────────────────────────────────── */

export type ExplanationBreach = { metric: string; value: number; threshold: number; exceeded_by: number; excess_unit: string }
export type ExplanationSample = {
  timestamp: string
  rtt_ms: number
  packet_loss_pct: number
  is_up: boolean
  stale?: boolean
  settings_pending?: boolean
  breaches: ExplanationBreach[]
  /** The server's verdict with the canonical predicates (fail | breach | marginal | clear), when it sends one. */
  classification?: string | null
}

export interface StatusExplanation {
  status: string
  mode: string
  source?: string
  /** The limits a sample is judged against; the recovery limits under either spelling (older servers send neither). */
  thresholds?: {
    degraded_rtt_ms?: number
    degraded_loss_pct?: number
    degraded_rtt_clear_ms?: number | null
    degraded_loss_clear_pct?: number | null
    clear_rtt_ms?: number | null
    clear_loss_pct?: number | null
  }
  confirmation?: { failed_checks?: number; packets_per_check?: number; interval_seconds?: number; recovery_checks?: number } | null
  policy?: (Partial<MonitoringPolicy> & { scope?: string }) | null
  state?: {
    hard_status?: string
    in_maintenance?: boolean
    pending?: PendingChange | null
    fail_streak?: number
    ok_streak?: number
    clear_streak?: number
    bad_in_window?: number
    window_len?: number
    window_size?: number
    status_since?: string | null
    last_sample_at?: string | null
    /** Silent longer than the gap rule: the next check starts the confirmation over (nothing is pending). */
    stale?: boolean
    /** No state yet (or another writer's): the owner seeds it silently from the device status at its next check. */
    seeded?: boolean
  } | null
  flapping?: {
    active?: boolean
    kind?: string | null
    since?: string | null
    reach_changes_in_window?: number
    perf_changes_in_window?: number
    window_minutes?: number
    start_changes?: number
    stop_changes?: number
    stable_minutes?: number
    enabled?: boolean
    earliest_stop_at?: string | null
    max_detectable_interval_s?: number | null
    /** False when this device's ping interval is too long for flapping to start. */
    detectable?: boolean
  } | null
  latest?: ExplanationSample | null
  recent_degraded?: { timestamp: string; reason: string; sample?: ExplanationSample | null; thresholds_unchanged: boolean; recovered_at?: string | null } | null
}

/** The policy an explanation describes: its `policy` block, else the legacy thresholds over the defaults. */
export function explanationPolicy(data: StatusExplanation): MonitoringPolicy {
  if (isRecord(data.policy)) return effectivePolicy(data.policy)
  const t = data.thresholds ?? {}
  return effectivePolicy({
    degraded_rtt_ms: t.degraded_rtt_ms,
    degraded_loss_pct: t.degraded_loss_pct,
    degraded_rtt_clear_ms: t.degraded_rtt_clear_ms ?? t.clear_rtt_ms ?? null,
    degraded_loss_clear_pct: t.degraded_loss_clear_pct ?? t.clear_loss_pct ?? null,
  })
}

/** Changes of the flapping class(es) in the window, for the badge tooltip. */
export function flapChangesInWindow(f: NonNullable<StatusExplanation['flapping']>): number | null {
  const kind = flapKindOf(f.kind)
  const reach = typeof f.reach_changes_in_window === 'number' ? f.reach_changes_in_window : null
  const perf = typeof f.perf_changes_in_window === 'number' ? f.perf_changes_in_window : null
  if (kind === 'reachability') return reach
  if (kind === 'performance') return perf
  if (reach === null && perf === null) return null
  return (reach ?? 0) + (perf ?? 0)
}

export type ExplanationTone = 'danger' | 'warning' | 'info'

export interface ExplanationView {
  /** Worth opening and highlighting: degraded, down, a pending change or flapping. */
  active: boolean
  tone: ExplanationTone
  heading: string
  /** The one-line summary under the compact widget's title. */
  summary: string
  /** "Down for 2h 14m" (confirmed status and its age), when known. */
  statusLine: string | null
  pending: string | null
  /** Flapping paragraph, when the device is flapping. */
  flapping: string | null
  rules: string[]
  /** Flap detection out of reach at this device's interval. */
  flapLimit: string | null
  /** The confirmation counters start over (a long silence) or have not started yet (a fresh seed). */
  stateNote: string | null
  maintenance: string | null
  scopeNote: string
}

const STATUS_WORD: Record<string, string> = { up: 'Up', degraded: 'Degraded', down: 'Down', unknown: 'Unknown', maintenance: 'Maintenance' }

/** Everything the explanation says, from the real policy and state. */
export function explanationView(data: StatusExplanation, nowMs: number, fmtTime: TimeFormatter = defaultTime): ExplanationView {
  const p = explanationPolicy(data)
  const state = data.state ?? {}
  const hard = state.hard_status || (data.status === 'maintenance' ? 'unknown' : data.status)
  const flap = data.flapping ?? null
  const flapping = flap?.active === true
  const downConfirm = typeof data.confirmation?.failed_checks === 'number' ? data.confirmation.failed_checks : p.down_confirm_checks
  // The checks actually in the window (it is cleared on every confirmed change), not its configured size.
  const inWindow = typeof state.window_len === 'number' && state.window_len > 0 ? state.window_len : null
  const pending = pendingText(state.pending ?? null, { hardStatus: hard, windowSize: inWindow ?? state.window_size ?? p.degraded_window_checks })
  const status = data.status
  const problem = status === 'down' ? 'down' : status === 'degraded' ? 'degraded' : null
  const heading = problem && flapping ? `Why this device is ${problem} and flapping`
    : problem ? `Why this device is ${problem}`
    : flapping ? 'Why this device is flapping'
    : pending ? 'Status change pending'
    : 'Reachability status details'
  const tone: ExplanationTone = status === 'down' ? 'danger'
    : status === 'degraded' || flapping || state.pending?.target === 'down' ? 'warning'
    : 'info'

  const since = parseApiTime(state.status_since)
  const statusLine = status === 'maintenance'
    ? null
    : since !== null
      ? `${STATUS_WORD[status] ?? status} for ${formatSpan(Math.max(0, (nowMs - since) / 1000))} (since ${fmtTime(since)})`
      : null

  let flapText: string | null = null
  if (flapping && flap) {
    const kind = flapKindOf(flap.kind)
    const changes = flapChangesInWindow(flap)
    const window = flap.window_minutes ?? p.flap_window_minutes
    const stable = flap.stable_minutes ?? p.flap_stable_minutes
    const stop = flap.stop_changes ?? p.flap_stop_changes
    const flapSince = parseApiTime(flap.since)
    const earliest = parseApiTime(flap.earliest_stop_at)
    flapText = [
      `Flapping${kind ? ` (${FLAP_KIND_LABEL[kind]} changes)` : ''}${flapSince !== null ? ` since ${fmtTime(flapSince)}` : ''}.`,
      changes !== null ? `${plural(changes, 'confirmed change')} in the last ${window} min (flapping starts at ${flap.start_changes ?? p.flap_start_changes}).` : '',
      `It ends after ${stable} min without a change${earliest !== null ? ` (${fmtTime(earliest)} at the earliest)` : ''} or when ${stop === 0 ? 'no change remains' : `at most ${plural(stop, 'change')} remain${stop === 1 ? 's' : ''}`} in the window.`,
      `${flapHeldText(kind, stable)} Interface, service and metric alerts are not affected.`,
    ].filter(Boolean).join(' ')
  }

  const s = policySentences(p, { downConfirm, packets: data.confirmation?.packets_per_check ?? null })
  const rules = [s.degraded, s.recovery, s.down, s.flapping]

  let flapLimit: string | null = null
  const interval = data.confirmation?.interval_seconds
  const maxS = typeof flap?.max_detectable_interval_s === 'number' ? flap.max_detectable_interval_s : null
  const undetectable = typeof flap?.detectable === 'boolean'
    ? !flap.detectable
    : typeof interval === 'number' && maxS !== null && interval > maxS
  if (p.flap_detection_enabled && !flapping && undetectable && typeof interval === 'number') {
    flapLimit = `At this device's ${fmtSeconds(interval)} ping interval flapping cannot be detected${maxS !== null ? ` (it needs an interval of ${fmtSeconds(maxS)} or less)` : ''}.`
  }

  // A silence longer than max(3 intervals, 3 min) resets the streaks and the window (the machine's gap rule).
  const gap = typeof interval === 'number' && interval > 0 ? Math.max(3 * interval, 180) : 180
  const stateNote = state.stale === true
    ? `No check for more than ${fmtSeconds(gap)}: the next check starts the confirmation counts over.`
    : state.seeded === true && status !== 'maintenance'
      ? 'The confirmed status is carried over from the device record; the confirmation counts start with the next check.'
      : null

  const maintenance = status === 'maintenance' || state.in_maintenance
    ? `A maintenance window is active. Checks keep being evaluated${hard && hard !== 'unknown' ? ` (confirmed status: ${STATUS_WORD[hard] ?? hard})` : ''}; the confirmed status is applied, with its notifications, when the window ends.`
    : null

  const flapKind = flapKindOf(flap?.kind)
  // Only the flapping kind is held: "Flapping (latency/loss); only latency/loss notifications held".
  const flapShort = flapping
    ? `Flapping${flapKind ? ` (${FLAP_KIND_LABEL[flapKind]})` : ''}; ${flapKind && flapKind !== 'both' ? 'only ' : ''}${flapHeldNoun(flapKind)} held`
    : null
  const latest = data.latest
  const lastCheck = latest
    ? `Last check ${fmtTime(parseApiTime(latest.timestamp) ?? 0)} · ${latest.is_up ? 'Responded' : 'No response'}`
    : 'No recent check available'
  const summary = [pending, flapShort].filter(Boolean).join(' · ') || lastCheck

  return {
    active: status === 'degraded' || status === 'down' || !!pending || flapping,
    tone,
    heading,
    summary,
    statusLine,
    pending,
    flapping: flapText,
    rules,
    flapLimit,
    stateNote,
    maintenance,
    scopeNote: data.policy?.scope === 'global' || !data.policy ? 'Global policy' : `Policy: ${data.policy.scope}`,
  }
}

export interface SampleVerdict { tone: 'success' | 'warning' | 'muted' | 'danger'; text: string }

/**
 * What the latest check means for the current status. While Degraded, both limits matter: a check between the
 * recovery limit and the Degraded limit does not degrade and restarts the consecutive recovery checks, but it is not
 * slow, lossy or unanswered, so it counts towards the clean-window recovery (device_status_machine.step).
 */
export function latestVerdict(sample: ExplanationSample, data: StatusExplanation): SampleVerdict | null {
  if (sample.stale) return { tone: 'warning', text: 'This sample is overdue and cannot explain the current network condition.' }
  if (sample.settings_pending) return { tone: 'muted', text: 'Threshold settings recently changed. Wait for the poller configuration refresh and next check.' }
  const p = explanationPolicy(data)
  const server = sample.classification
  const cls: CheckClass = server === 'fail' || server === 'breach' || server === 'marginal' || server === 'clear'
    ? server
    : classifyCheck(sample, p)
  const hard = data.state?.hard_status || data.status
  if (cls === 'fail') {
    return { tone: 'warning', text: hard === 'down' ? 'The latest check received no response.' : 'The latest check received no response; it counts towards Down and Degraded.' }
  }
  if (cls === 'breach') return null // the breaches themselves are listed
  if (hard === 'degraded') {
    const limits = `(${fmt3(p.degraded_rtt_clear_ms)} ms, ${fmt3(p.degraded_loss_clear_pct)}%)`
    if (cls === 'clear') return { tone: 'success', text: `Both measurements are at or below the recovery limits ${limits}; this check counts towards recovery.` }
    const win = p.degraded_window_checks
    if (win === 1) return { tone: 'muted', text: `Within the Degraded limits but above a recovery limit ${limits}: with a window of one check, this check is enough for recovery.` }
    const streak = p.degraded_clear_checks === 1
      ? 'is not a recovery check on its own'
      : `restarts the count of ${p.degraded_clear_checks} consecutive checks within the recovery limits`
    return { tone: 'muted', text: `Within the Degraded limits but above a recovery limit ${limits}: this check does not degrade and ${streak}, but it counts towards recovery after ${win} checks in a row with no slow, lossy or unanswered check.` }
  }
  return { tone: 'success', text: 'Both measurements are within the Degraded limits.' }
}

/** The limit(s) printed beside a measurement: the Degraded limit, plus the recovery limit while Degraded. */
export function limitText(metric: 'latency' | 'loss', data: StatusExplanation): string {
  const p = explanationPolicy(data)
  const degraded = (data.state?.hard_status || data.status) === 'degraded'
  if (metric === 'latency') {
    return degraded
      ? `Degraded above ${fmt3(p.degraded_rtt_ms)} ms · recovers at or below ${fmt3(p.degraded_rtt_clear_ms)} ms`
      : `${fmt3(p.degraded_rtt_ms)} ms limit`
  }
  return degraded
    ? `Degraded above ${fmt3(p.degraded_loss_pct)}% · recovers at or below ${fmt3(p.degraded_loss_clear_pct)}%`
    : `${fmt3(p.degraded_loss_pct)}% limit`
}

/* ── Alerts ──────────────────────────────────────────────────────────────── */

export type AlertFlapState =
  | { kind: 'held'; label: string; until: string | null; title: string }
  | { kind: 'notice'; event: 'start' | 'stop'; label: string; title: string; detail: string }

/**
 * Flap presentation of an alert row: a pending trigger held because its device is flapping (the row's `flap_held` and
 * `hold_until`, or the same keys in its metadata for older servers), or a flap notice row (metadata.flap_notice,
 * flap_event 'start'|'stop', the episode's `kind` and, on a start, `stable_minutes`): a notice names the kind of change
 * whose notifications are held or resumed, never every status notification.
 */
export function alertFlapState(
  a: { status?: string | null; metadata?: unknown; flap_held?: boolean | null; hold_until?: string | null },
  fmtTime: TimeFormatter = defaultTime,
): AlertFlapState | null {
  const meta = isRecord(a.metadata) ? a.metadata : {}
  if (meta.flap_notice === true) {
    const event = meta.flap_event === 'stop' ? 'stop' : 'start'
    const kind = flapKindOf(meta.kind)
    const what = kind ? ` (${FLAP_KIND_LABEL[kind]} changes)` : ''
    if (event === 'start') {
      const held = flapHeldText(kind, typeof meta.stable_minutes === 'number' ? meta.stable_minutes : null)
      return { kind: 'notice', event, label: 'Flapping started', title: `Notice: the device started flapping${what}. ${held}`, detail: `This notice opens no incident. ${held}` }
    }
    const resumed = `${capitalize(flapHeldNoun(kind))} for the device have resumed.`
    return { kind: 'notice', event, label: 'Flapping stopped', title: `Notice: the device stopped flapping${what}. ${resumed}`, detail: `This notice opens no incident. ${resumed}` }
  }
  if (a.status === 'pending' && (a.flap_held === true || meta.flap_held === true)) {
    const until = typeof a.hold_until === 'string' ? a.hold_until : typeof meta.hold_until === 'string' ? meta.hold_until : null
    const at = parseApiTime(until)
    return {
      kind: 'held',
      label: 'Held: device flapping',
      until,
      title: `Held because the device is flapping${at !== null ? `: sent at ${fmtTime(at)} if the device is still in this status` : ': sent if the device is still in this status when the hold ends'}, otherwise closed without a notification.`,
    }
  }
  return null
}

/* ── Activity log: transitions inside a flap episode ─────────────────────────────────────────────────────────────── */

/** One row of GET /devices/{id}/flap-episodes. */
export interface FlapEpisode {
  id?: string
  started_at: string
  ended_at?: string | null
  kind?: string | null
  start_changes?: number | null
  window_minutes?: number | null
  status_at_start?: string | null
  status_at_end?: string | null
  end_reason?: string | null
}

export interface StatusTransitionLike { timestamp: string; old_status?: string | null; new_status?: string | null }

export const FLAP_END_REASON_LABEL: Record<string, string> = {
  stable: 'status stable',
  disabled: 'flap detection turned off',
  maintenance: 'maintenance window',
  owner_change: 'polling owner changed or went offline',
  reseed: 'status reset by another writer',
  not_polled: 'device no longer polled',
}

/** Reachability (into/out of down), performance (up <-> degraded) or neither (unknown, maintenance). */
export function transitionClass(oldStatus: string | null | undefined, newStatus: string | null | undefined): 'reachability' | 'performance' | null {
  const o = (oldStatus || '').toLowerCase()
  const n = (newStatus || '').toLowerCase()
  const real = (s: string) => s === 'up' || s === 'degraded' || s === 'down'
  if (!real(o) || !real(n) || o === n) return null
  if (o === 'down' || n === 'down') return 'reachability'
  return 'performance'
}

/**
 * Whether a transition is part of an episode: of a class that flapped, between the start of the window that
 * started it (the changes that made it flap) and the episode's end (now while it is open).
 */
export function episodeCovers(ep: FlapEpisode, t: StatusTransitionLike, nowMs: number): boolean {
  const cls = transitionClass(t.old_status, t.new_status)
  const kind = flapKindOf(ep.kind)
  if (!cls || (kind && kind !== 'both' && kind !== cls)) return false
  const at = parseApiTime(t.timestamp)
  const start = parseApiTime(ep.started_at)
  if (at === null || start === null) return false
  const lead = typeof ep.window_minutes === 'number' && ep.window_minutes > 0 ? ep.window_minutes * 60_000 : 0
  const end = parseApiTime(ep.ended_at) ?? nowMs
  return at > start - lead && at <= end
}

/**
 * Where a covered transition sits in its episode. 'lead-in': one of the changes that made the class flap, before the
 * episode started; the class was not flapping yet, so its notifications went out as usual (the machine's transition
 * flag is false). 'during': at or after the start (the starting change itself carries the flag).
 */
export type FlapPhase = 'lead-in' | 'during'

export function flapPhase(ep: FlapEpisode, t: StatusTransitionLike): FlapPhase {
  const at = parseApiTime(t.timestamp)
  const start = parseApiTime(ep.started_at)
  // Both are whole milliseconds (Date.parse truncates finer fractions), so the start change compares equal.
  return at !== null && start !== null && at < start ? 'lead-in' : 'during'
}

export type ActivityGroup<T> =
  | { kind: 'transition'; item: T; episode: FlapEpisode | null; phase: FlapPhase | null }
  | { kind: 'episode'; episode: FlapEpisode; items: T[] }

/**
 * Status transitions with those inside a flap episode gathered under it (each episode once, also when none of its
 * transitions is in the list). `collapse` false keeps every transition, marked with its episode and phase, for the
 * full log.
 */
export function groupFlapTransitions<T extends StatusTransitionLike>(history: readonly T[], episodes: readonly FlapEpisode[], nowMs: number, collapse = true): ActivityGroup<T>[] {
  const ordered = [...episodes].filter((e) => parseApiTime(e.started_at) !== null)
    .sort((a, b) => (parseApiTime(a.started_at) ?? 0) - (parseApiTime(b.started_at) ?? 0))
  const members = new Map<FlapEpisode, T[]>(ordered.map((e) => [e, []]))
  const out: ActivityGroup<T>[] = []
  for (const t of history) {
    const ep = ordered.find((e) => episodeCovers(e, t, nowMs)) ?? null
    if (ep) members.get(ep)!.push(t)
    if (!ep || !collapse) out.push({ kind: 'transition', item: t, episode: ep, phase: ep ? flapPhase(ep, t) : null })
  }
  if (collapse) for (const ep of ordered) out.push({ kind: 'episode', episode: ep, items: members.get(ep)! })
  return out
}

/**
 * The single row an episode becomes in the recent-events widget. The span is the flapping itself (from the episode's
 * start); the lead-in changes that made it flap were notified as usual and are mentioned separately.
 */
export function episodeSummary(ep: FlapEpisode, items: readonly StatusTransitionLike[], nowMs: number, fmtTime: TimeFormatter = defaultTime): { title: string; subtitle: string; timestamp: string } {
  const kind = flapKindOf(ep.kind)
  const times = items.map((t) => parseApiTime(t.timestamp)).filter((t): t is number => t !== null).sort((a, b) => a - b)
  const start = parseApiTime(ep.started_at) ?? nowMs
  const end = parseApiTime(ep.ended_at)
  const leadIn = times.filter((t) => t < start)
  const last = Math.max(end ?? 0, times.length ? times[times.length - 1] : 0, start)
  const title = `Flapping${kind ? ` (${FLAP_KIND_LABEL[kind]})` : ''}${items.length ? `: ${plural(items.length, 'status change')}` : ''}`
  const span = `${fmtTime(start)}–${end !== null ? fmtTime(end) : 'now'}`
  const led = leadIn.length ? `${plural(leadIn.length, 'earlier change')} from ${fmtTime(leadIn[0])} led to it` : ''
  const state = end !== null
    ? `ended${ep.end_reason ? ` (${FLAP_END_REASON_LABEL[ep.end_reason] ?? ep.end_reason})` : ''}`
    : 'ongoing'
  return { title, subtitle: [span, led, state, `${flapHeldNoun(kind)} held while flapping`].filter(Boolean).join(' · '), timestamp: new Date(last).toISOString() }
}
