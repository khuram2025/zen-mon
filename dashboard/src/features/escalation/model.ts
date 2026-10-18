/**
 * Escalation & on-call — pure logic shared by the pages and the tests
 * (scripts/test-escalation-ui.mjs loads this file on its own, so it imports
 * types only).
 *
 * Binding product rules (work/ESCALATION-PLAN.md):
 *  - Priority P1 Critical, P2 High, P3 Warning, P4 Information. A rule with no
 *    priority derives it from severity: critical -> P2, warning -> P3,
 *    info -> P4. P1 is only ever chosen explicitly.
 *  - Routing precedence: the rule's own enabled policy > the rule's own legacy
 *    tiers > the enabled policy that is the default for the alert priority >
 *    nothing. A priority default never takes over a rule that has tiers.
 *  - Step 0 (delay 0) pages in addition to the rule's own channels; later
 *    steps fire at their delay while the incident is active and unacknowledged.
 *  - Only P3/P4 trigger notifications are ever grouped; P1/P2, recoveries,
 *    escalations and reviews are never held.
 */
import type {
  ContactKind, ContactMethod, Coverage, CoverageEntry, Digest, DigestDetail, DigestItem, EscalationPolicy, GroupBy,
  GroupingSettings, NamedRef, NotificationHold, OnCallNow, OncallLayer, OncallOverride, OncallSchedule, PolicyDraft,
  PolicyPreview, PolicyStep, PreviewRecipient, PreviewStep, Priority, ResponseTargets, Restriction, RotationPreset,
  RouteExplanation, RouteSource, ScheduleDraft, Shift, Target, TargetType,
} from './types'

/* ── Generic helpers ──────────────────────────────────────────────────── */

const isObj = (v: unknown): v is Record<string, any> => !!v && typeof v === 'object' && !Array.isArray(v)
const str = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : String(v))
const strOrNull = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v : null)

/** A list response in any of the shapes the API uses: [..], {items}, {data}, {results}. */
export function asList<T = any>(raw: unknown, key?: string): T[] {
  if (Array.isArray(raw)) return raw as T[]
  if (!isObj(raw)) return []
  for (const k of [key, 'items', 'data', 'results', 'policies', 'schedules', 'methods'].filter(Boolean) as string[]) {
    if (Array.isArray(raw[k])) return raw[k] as T[]
  }
  return []
}

/** Positive whole minutes or null ("" / 0 / junk = none). */
export function minutesOrNull(v: unknown): number | null {
  if (v == null || v === '' || typeof v === 'boolean') return null
  const n = typeof v === 'number' ? v : Number(String(v).trim())
  if (!Number.isFinite(n) || n <= 0) return null
  return Math.round(n)
}

function intOr(v: unknown, fallback: number): number {
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? Math.round(n) : fallback
}

/** `5 min`, `1 h`, `1 h 30 min`, `4 h`, `2 d`, `1 d 6 h`; null -> '—'. */
export function fmtMinutes(m: number | null | undefined): string {
  if (m == null || !Number.isFinite(m)) return '—'
  const v = Math.max(0, Math.round(m))
  if (v === 0) return '0 min'
  if (v < 60) return `${v} min`
  if (v < 1440) {
    const h = Math.floor(v / 60)
    const r = v % 60
    return r ? `${h} h ${r} min` : `${h} h`
  }
  const d = Math.floor(v / 1440)
  const h = Math.floor((v % 1440) / 60)
  return h ? `${d} d ${h} h` : `${d} d`
}

/** `T+0`, `T+5 min`, `T+4 h` — offsets on a step timeline. */
export function fmtOffset(m: number): string {
  return m <= 0 ? 'At trigger' : `+${fmtMinutes(m)}`
}

/** `45 s`, `5 min`, `1 h` for second-based settings. */
export function fmtSeconds(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return '—'
  const v = Math.max(0, Math.round(s))
  if (v < 60) return `${v} s`
  if (v % 60 === 0) return fmtMinutes(v / 60)
  return `${Math.floor(v / 60)} min ${v % 60} s`
}

/* ── Priority ─────────────────────────────────────────────────────────── */

export const PRIORITIES: readonly Priority[] = ['P1', 'P2', 'P3', 'P4']

export type PriorityMeta = {
  name: string
  label: string
  tone: 'danger' | 'warning' | 'info' | 'muted'
  /** Trigger description from the field-report matrix. */
  trigger: string
  /** Initial owner and channel (report matrix). */
  owner: string
  /** Acknowledgement and escalation (report matrix). */
  escalation: string
  /** Review cadence (report matrix). */
  review: string
}

export const PRIORITY_META: Record<Priority, PriorityMeta> = {
  P1: {
    name: 'Critical',
    label: 'P1 Critical',
    tone: 'danger',
    trigger: 'Confirmed major outage, severe service impact or imminent loss of essential capacity.',
    owner: 'Primary on-call plus NOC. Email and an independently tested urgent channel, where enabled.',
    escalation: 'Acknowledge within 5 min. At 5 min notify backup on-call. At 15 min notify incident lead. At 30 min apply the major-incident policy.',
    review: 'Update every 15 min while unresolved.',
  },
  P2: {
    name: 'High',
    label: 'P2 High',
    tone: 'warning',
    trigger: 'Persistent degradation or loss of redundancy with material impact.',
    owner: 'Owning service team and NOC. Email, with urgent channel according to service policy.',
    escalation: 'Acknowledge within 15 min. At 15 min notify backup. At 30 min notify team lead.',
    review: 'Review unresolved impact at 60 min; updates every 30 min.',
  },
  P3: {
    name: 'Warning',
    label: 'P3 Warning',
    tone: 'info',
    trigger: 'Sustained resource threshold or localized non-critical errors.',
    owner: 'Owning team queue. Email or grouped digest; business-hours coverage where agreed.',
    escalation: 'Acknowledge within 60 staffed min. If unowned after 4 staffed hours, notify team lead.',
    review: 'Periodic summaries rather than repeated new alerts.',
  },
  P4: {
    name: 'Information',
    label: 'P4 Information',
    tone: 'muted',
    trigger: 'Recovery, expected reboot or routine state change.',
    owner: 'Audit timeline and subscribed report recipients.',
    escalation: 'No paging escalation.',
    review: 'Included in digests unless a recipient subscribes to immediate lifecycle notices.',
  },
}

/** 'p2' / 'P2' / 2 -> 'P2'; anything else -> null. */
export function normalizePriority(v: unknown): Priority | null {
  if (typeof v === 'number' && Number.isInteger(v)) v = `P${v}`
  if (typeof v !== 'string') return null
  const p = v.trim().toUpperCase()
  return (PRIORITIES as readonly string[]).includes(p) ? (p as Priority) : null
}

/** Priority derived from severity alone; P1 is never derived. */
export function derivePriority(severity: unknown): Priority {
  const s = str(severity).toLowerCase()
  return s === 'critical' ? 'P2' : s === 'info' ? 'P4' : 'P3'
}

/** A rule's explicit priority, else derived from its severity. */
export function effectivePriority(rulePriority: unknown, severity: unknown): Priority {
  return normalizePriority(rulePriority) ?? derivePriority(severity)
}

/** Text for the rule wizard's "Auto" option. */
export function autoPriorityText(severity: unknown): string {
  const p = derivePriority(severity)
  return `Auto from severity — ${str(severity) || 'warning'} rules raise ${PRIORITY_META[p].label}.`
}

/** Urgent priorities are never grouped, held or budgeted. */
export function isUrgent(p: Priority | null | undefined): boolean {
  return p === 'P1' || p === 'P2'
}

/* ── Response targets ─────────────────────────────────────────────────── */

export const DEFAULT_RESPONSE_TARGETS: ResponseTargets = { P1: 5, P2: 15, P3: 60, P4: null }

/** P-keys win; a missing P-key reads the legacy severity key that derives to it. */
export function targetsFromApi(raw: unknown): ResponseTargets {
  const out: ResponseTargets = { ...DEFAULT_RESPONSE_TARGETS }
  if (!isObj(raw)) return out
  const src = isObj(raw.targets) ? raw.targets : raw
  const legacy: Record<string, Priority> = { critical: 'P2', warning: 'P3', info: 'P4' }
  for (const [sev, p] of Object.entries(legacy)) if (sev in src) out[p] = minutesOrNull(src[sev])
  for (const p of PRIORITIES) if (p in src) out[p] = minutesOrNull(src[p])
  return out
}

export function validateTargetMinutes(text: string): string | null {
  if (text.trim() === '') return null
  const n = Number(text)
  if (!Number.isInteger(n) || n < 1 || n > 10080) return 'Whole minutes from 1 to 10080, or empty for no target'
  return null
}

/* ── Targets ──────────────────────────────────────────────────────────── */

export type Directory = {
  schedules: Array<{ id: string; name: string; enabled?: boolean; members?: number }>
  users: Array<{ id: string; name: string; username?: string }>
  channels: Array<{ id: string; name: string; type?: string; enabled?: boolean }>
}

export const EMPTY_DIRECTORY: Directory = { schedules: [], users: [], channels: [] }

export const TARGET_TYPE_LABEL: Record<TargetType, string> = { schedule: 'On-call schedule', user: 'User', channel: 'Channel' }

export const targetKey = (t: Target) => `${t.type}:${t.id}`

export function normalizeTarget(raw: unknown): Target | null {
  if (!isObj(raw)) return null
  const type = raw.type as TargetType
  if (type !== 'schedule' && type !== 'user' && type !== 'channel') return null
  const id = str(raw.id)
  if (!id) return null
  return { type, id, name: strOrNull(raw.name) ?? strOrNull(raw.label) }
}

/** Adds a target once (same type + id = same target). */
export function addTarget(list: Target[], t: Target): Target[] {
  return list.some((x) => targetKey(x) === targetKey(t)) ? list : [...list, { type: t.type, id: t.id }]
}

export function removeTarget(list: Target[], t: Target): Target[] {
  return list.filter((x) => targetKey(x) !== targetKey(t))
}

export type TargetInfo = { label: string; kindLabel: string; missing: boolean; disabled: boolean; detail: string | null }

/** Name and state of a target from the directory (or its embedded name). */
export function describeTarget(t: Target, dir: Directory): TargetInfo {
  const kindLabel = TARGET_TYPE_LABEL[t.type]
  if (t.type === 'schedule') {
    const s = dir.schedules.find((x) => x.id === t.id)
    if (s) {
      const empty = s.members === 0
      return { label: s.name, kindLabel, missing: false, disabled: s.enabled === false, detail: empty ? 'No members' : null }
    }
  } else if (t.type === 'user') {
    const u = dir.users.find((x) => x.id === t.id)
    if (u) return { label: u.name, kindLabel, missing: false, disabled: false, detail: null }
  } else {
    const c = dir.channels.find((x) => x.id === t.id)
    if (c) return { label: c.name, kindLabel, missing: false, disabled: c.enabled === false, detail: c.type ? c.type.toUpperCase() : null }
  }
  if (t.name) return { label: t.name, kindLabel, missing: false, disabled: false, detail: null }
  // Channel names need alerts.manage (the channel directory); a read-only role sees the kind only.
  const known = t.type === 'channel' ? dir.channels.length > 0 : t.type === 'user' ? dir.users.length > 0 : dir.schedules.length > 0
  return { label: known ? `Deleted ${kindLabel.toLowerCase()}` : kindLabel, kindLabel, missing: known, disabled: false, detail: null }
}

export type PickerOption = { type: TargetType; id: string; label: string; detail: string | null; disabled: boolean }

/** Options for the targets picker, filtered by a search text and target types. */
export function pickerOptions(dir: Directory, query: string, types: readonly TargetType[] = ['schedule', 'user', 'channel'], exclude: Target[] = []): PickerOption[] {
  const q = query.trim().toLowerCase()
  const taken = new Set(exclude.map(targetKey))
  const out: PickerOption[] = []
  const push = (o: PickerOption, hay: string) => {
    if (taken.has(`${o.type}:${o.id}`)) return
    if (q && !hay.toLowerCase().includes(q)) return
    out.push(o)
  }
  if (types.includes('schedule')) {
    for (const s of dir.schedules) push({ type: 'schedule', id: s.id, label: s.name, detail: s.members === 0 ? 'No members' : 'On-call schedule', disabled: s.enabled === false }, s.name)
  }
  if (types.includes('user')) {
    for (const u of dir.users) push({ type: 'user', id: u.id, label: u.name, detail: u.username && u.username !== u.name ? u.username : 'User', disabled: false }, `${u.name} ${u.username ?? ''}`)
  }
  if (types.includes('channel')) {
    for (const c of dir.channels) push({ type: 'channel', id: c.id, label: c.name, detail: c.type ? `${c.type.toUpperCase()} channel` : 'Channel', disabled: c.enabled === false }, `${c.name} ${c.type ?? ''}`)
  }
  return out
}

/* ── Policies ─────────────────────────────────────────────────────────── */

/* Server limits (escalation_policy_service.py). */
export const MAX_STEPS = 10
export const MAX_DELAY = 10080
export const MAX_REPEATS = 20
export const MAX_STEP_TARGETS = 20
export const MAX_FALLBACK_TARGETS = 10

function stepFromApi(raw: unknown, i: number): PolicyStep {
  const r = isObj(raw) ? raw : {}
  return {
    id: strOrNull(r.id) ?? undefined,
    position: intOr(r.position, i),
    delay_minutes: Math.max(0, intOr(r.delay_minutes, 0)),
    name: strOrNull(r.name),
    targets: asList(r.targets).map(normalizeTarget).filter(Boolean) as Target[],
  }
}

export function policyFromApi(raw: unknown): EscalationPolicy {
  const r = isObj(raw) ? raw : {}
  const steps = asList(r.steps).map(stepFromApi).sort((a, b) => a.position - b.position || a.delay_minutes - b.delay_minutes)
  return {
    id: str(r.id),
    name: str(r.name) || 'Untitled policy',
    description: strOrNull(r.description),
    enabled: r.enabled !== false,
    default_priorities: asList(r.default_priorities).map(normalizePriority).filter(Boolean) as Priority[],
    ack_target_minutes: minutesOrNull(r.ack_target_minutes),
    repeat_minutes: minutesOrNull(r.repeat_minutes),
    max_repeats: Math.max(0, intOr(r.max_repeats, 3)),
    review_minutes: minutesOrNull(r.review_minutes),
    ack_expiry_minutes: minutesOrNull(r.ack_expiry_minutes),
    fallback_targets: asList(r.fallback_targets).map(normalizeTarget).filter(Boolean) as Target[],
    steps,
    used_by_rules: Math.max(0, intOr(r.used_by_rules, 0)),
    updated_at: strOrNull(r.updated_at),
    created_at: strOrNull(r.created_at),
  }
}

let stepSeq = 0
const nextKey = (p: string) => `${p}-${++stepSeq}`

export function emptyPolicyDraft(): PolicyDraft {
  return {
    name: '',
    description: '',
    enabled: true,
    default_priorities: [],
    ack_target_minutes: '',
    repeat_minutes: '',
    max_repeats: '3',
    review_minutes: '',
    ack_expiry_minutes: '',
    fallback_targets: [],
    steps: [{ key: nextKey('s'), delay_minutes: '0', name: 'Initial page', targets: [] }],
  }
}

const numText = (v: number | null) => (v == null ? '' : String(v))

export function draftFromPolicy(p: EscalationPolicy): PolicyDraft {
  return {
    name: p.name,
    description: p.description ?? '',
    enabled: p.enabled,
    default_priorities: [...p.default_priorities],
    ack_target_minutes: numText(p.ack_target_minutes),
    repeat_minutes: numText(p.repeat_minutes),
    max_repeats: String(p.max_repeats),
    review_minutes: numText(p.review_minutes),
    ack_expiry_minutes: numText(p.ack_expiry_minutes),
    fallback_targets: p.fallback_targets.map((t) => ({ type: t.type, id: t.id })),
    steps: p.steps.map((s) => ({ key: nextKey('s'), delay_minutes: String(s.delay_minutes), name: s.name ?? '', targets: s.targets.map((t) => ({ type: t.type, id: t.id })) })),
  }
}

/** The API body for a draft (POST / PUT /escalation-policies). */
export function policyPayload(d: PolicyDraft) {
  const steps = d.steps
    .map((s) => ({ delay_minutes: Math.max(0, Math.round(Number(s.delay_minutes) || 0)), name: s.name.trim() || null, targets: s.targets.map((t) => ({ type: t.type, id: t.id })) }))
    .sort((a, b) => a.delay_minutes - b.delay_minutes)
    .map((s, i) => ({ position: i, ...s }))
  const maxRepeats = Number(d.max_repeats)
  return {
    name: d.name.trim(),
    description: d.description.trim() || null,
    enabled: d.enabled,
    default_priorities: PRIORITIES.filter((p) => d.default_priorities.includes(p)),
    ack_target_minutes: minutesOrNull(d.ack_target_minutes),
    repeat_minutes: minutesOrNull(d.repeat_minutes),
    max_repeats: Number.isFinite(maxRepeats) && maxRepeats >= 0 ? Math.round(maxRepeats) : 0,
    review_minutes: minutesOrNull(d.review_minutes),
    ack_expiry_minutes: minutesOrNull(d.ack_expiry_minutes),
    fallback_targets: d.fallback_targets.map((t) => ({ type: t.type, id: t.id })),
    steps,
  }
}

/** Changed relative to the saved version (key order and step keys ignored). */
export function policyDirty(draft: PolicyDraft, saved: PolicyDraft): boolean {
  return JSON.stringify(policyPayload(draft)) !== JSON.stringify(policyPayload(saved))
}

export type PolicyErrors = Partial<Record<'name' | 'ack_target_minutes' | 'repeat_minutes' | 'max_repeats' | 'review_minutes' | 'ack_expiry_minutes' | 'steps', string>> & {
  stepErrors?: Record<string, string>
}

function minutesField(text: string, max: number, label: string, min = 1): string | null {
  if (text.trim() === '') return null
  const n = Number(text)
  if (!Number.isInteger(n) || n < min || n > max) return `${label}: whole minutes from ${min} to ${max}, or empty`
  return null
}

export function validatePolicy(d: PolicyDraft): PolicyErrors {
  const e: PolicyErrors = {}
  if (!d.name.trim()) e.name = 'Name the policy'
  else if (d.name.trim().length > 255) e.name = 'At most 255 characters'
  const a = minutesField(d.ack_target_minutes, 10080, 'Acknowledgement target'); if (a) e.ack_target_minutes = a
  const r = minutesField(d.repeat_minutes, 1440, 'Repeat'); if (r) e.repeat_minutes = r
  const rv = minutesField(d.review_minutes, 1440, 'Unresolved updates', 5); if (rv) e.review_minutes = rv
  const x = minutesField(d.ack_expiry_minutes, 10080, 'Acknowledgement expiry', 5); if (x) e.ack_expiry_minutes = x
  const mr = Number(d.max_repeats)
  if (d.max_repeats.trim() === '' || !Number.isInteger(mr) || mr < 0 || mr > MAX_REPEATS) e.max_repeats = `Whole number from 0 to ${MAX_REPEATS}`
  // No steps is allowed (an audit-only P4 policy); the outline warns about it.
  if (d.steps.length > MAX_STEPS) e.steps = `At most ${MAX_STEPS} steps`
  const stepErrors: Record<string, string> = {}
  const seen = new Map<number, string>()
  for (const s of d.steps) {
    const n = Number(s.delay_minutes)
    if (s.delay_minutes.trim() === '' || !Number.isInteger(n) || n < 0 || n > MAX_DELAY) stepErrors[s.key] = `Delay: whole minutes from 0 to ${MAX_DELAY}`
    else if (seen.has(n)) stepErrors[s.key] = 'Two steps cannot fire at the same delay'
    else if (!s.targets.length) stepErrors[s.key] = 'Pick at least one schedule, user or channel'
    else if (s.targets.length > MAX_STEP_TARGETS) stepErrors[s.key] = `At most ${MAX_STEP_TARGETS} targets per step`
    else if (s.name.trim().length > 100) stepErrors[s.key] = 'Step name: at most 100 characters'
    if (Number.isInteger(n)) seen.set(n, s.key)
  }
  if (Object.keys(stepErrors).length) e.stepErrors = stepErrors
  if (d.fallback_targets.length > MAX_FALLBACK_TARGETS) e.steps = `At most ${MAX_FALLBACK_TARGETS} fallback targets`
  return e
}

export const hasErrors = (e: PolicyErrors) => Object.keys(e).some((k) => k !== 'stepErrors') || !!(e.stepErrors && Object.keys(e.stepErrors).length)

/** The next step's default delay: double the last, or 15 minutes. */
export function nextStepDelay(d: PolicyDraft): number {
  const delays = d.steps.map((s) => Number(s.delay_minutes)).filter((n) => Number.isFinite(n))
  const last = delays.length ? Math.max(...delays) : -1
  if (last < 0) return 0
  if (last === 0) return 15
  return Math.min(MAX_DELAY, last * 2)
}

export function newStep(d: PolicyDraft) {
  return { key: nextKey('s'), delay_minutes: String(nextStepDelay(d)), name: '', targets: [] as Target[] }
}

export type OutlineStep = {
  key: string
  delay: number
  name: string
  targets: Array<Target & TargetInfo>
  gaps: string[]
}

/** What the draft would do, from the directory alone (no server call). */
export function draftOutline(d: PolicyDraft, dir: Directory): { steps: OutlineStep[]; warnings: string[] } {
  const steps = [...d.steps]
    .map((s) => ({ s, delay: Number(s.delay_minutes) }))
    .sort((a, b) => (Number.isFinite(a.delay) ? a.delay : 0) - (Number.isFinite(b.delay) ? b.delay : 0))
    .map(({ s, delay }, i) => {
      const targets = s.targets.map((t) => ({ ...t, ...describeTarget(t, dir) }))
      const gaps: string[] = []
      if (!targets.length) gaps.push('No targets — nobody is notified at this step')
      for (const t of targets) {
        if (t.missing) gaps.push(`${t.label} no longer exists`)
        else if (t.disabled) gaps.push(`${t.label} is disabled`)
        else if (t.detail === 'No members') gaps.push(`${t.label} has no members`)
      }
      return { key: s.key, delay: Number.isFinite(delay) ? delay : 0, name: s.name.trim() || `Step ${i + 1}`, targets, gaps }
    })
  const warnings: string[] = []
  if (!steps.length) warnings.push('No steps: this policy pages nobody (audit only). Incidents still notify the rule’s own channels.')
  if (steps.length && steps[0].delay > 0) {
    warnings.push(`The first step fires after ${fmtMinutes(steps[0].delay)}; until then only the rule's own channels are notified.`)
  }
  if (!d.fallback_targets.length) warnings.push('No fallback route — a step that reaches nobody, or whose deliveries all fail, records a coverage gap only.')
  const ack = minutesOrNull(d.ack_target_minutes)
  const expiry = minutesOrNull(d.ack_expiry_minutes)
  if (minutesOrNull(d.repeat_minutes) && Number(d.max_repeats) === 0) warnings.push('A repeat interval is set but max repeats is 0, so steps never repeat.')
  if (ack != null && steps.length > 1 && steps[1].delay > 0 && steps[1].delay < ack) {
    warnings.push(`Step 2 fires at ${fmtMinutes(steps[1].delay)}, before the ${fmtMinutes(ack)} acknowledgement target.`)
  }
  if (expiry != null && ack != null && expiry < ack) warnings.push('The acknowledgement expires sooner than the acknowledgement target.')
  if (d.default_priorities.some((p) => p === 'P1' || p === 'P2') && minutesOrNull(d.review_minutes) == null) {
    warnings.push('P1/P2 policies normally send unresolved-incident updates (review interval).')
  }
  return { steps, warnings }
}

/**
 * The order the server uses when several enabled policies claim one priority
 * (escalation_policy_service: ORDER BY created_at, id): oldest first. Without
 * created_at on both, by name (then id) so the choice is still stable.
 */
function defaultClaimOrder(a: EscalationPolicy, b: EscalationPolicy): number {
  const ta = a.created_at ? Date.parse(a.created_at) : NaN
  const tb = b.created_at ? Date.parse(b.created_at) : NaN
  if (Number.isFinite(ta) && Number.isFinite(tb)) {
    if (ta !== tb) return ta - tb
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0
  }
  return a.name.localeCompare(b.name) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
}

/** Default policy for a priority: the enabled policy whose default_priorities contains it (the oldest when several claim it). */
export function policyForPriority(policies: EscalationPolicy[], p: Priority): EscalationPolicy | null {
  return [...policies]
    .filter((x) => x.enabled && x.default_priorities.includes(p))
    .sort(defaultClaimOrder)[0] ?? null
}

/**
 * Priorities claimed as default by more than one enabled policy, in the order the server picks them (the first is used).
 * The server refuses to save a second enabled claim (409), so these only come from older data.
 */
export function priorityConflicts(policies: EscalationPolicy[]): Array<{ priority: Priority; policies: string[] }> {
  return PRIORITIES.map((p) => ({ priority: p, policies: policies.filter((x) => x.enabled && x.default_priorities.includes(p)).sort(defaultClaimOrder).map((x) => x.name) }))
    .filter((x) => x.policies.length > 1)
}

/** Shown next to a default-priority conflict: the server refuses the save (409) while another enabled policy claims it. */
export const DEFAULT_CONFLICT_REFUSAL = 'saving will be refused unless the other policy drops it'

/* ── Rule escalation (the rule wizard and the rule list) ──────────────── */

/**
 * What the server routes a saved rule's incidents through: the rule's own
 * enabled policy > the rule's own legacy tiers > the priority default policy >
 * nothing. `policyEnabled` is unknown (undefined) when the policy list is not
 * loaded; the server then still has the policy, so it counts as enabled.
 */
export function ruleRouteSource(r: { policyId?: string | null; policyEnabled?: boolean; tiers: number; defaultPolicy: EscalationPolicy | null }): RouteSource {
  if (r.policyId && r.policyEnabled !== false) return 'rule'
  if (r.tiers > 0) return 'legacy_levels'
  if (r.defaultPolicy) return 'priority_default'
  return 'none'
}

const tierCount = (n: number) => `${n} legacy tier${n === 1 ? '' : 's'}`

/** Legacy-tiers mode: the tiers win over the priority default; with no tiers the default (or nothing) applies. */
export function legacyTiersText(tiers: number, priority: Priority, defaultPolicy: EscalationPolicy | null): string {
  if (tiers > 0) {
    return defaultPolicy
      ? `These tiers take precedence over the ${priority} default policy (${defaultPolicy.name}).`
      : `No policy is the default for ${priority}; these tiers are this rule's only escalation.`
  }
  return defaultPolicy
    ? `No tiers yet: without one, the ${priority} default policy (${defaultPolicy.name}) applies.`
    : `No tiers yet: without one, this rule's incidents do not escalate (no policy is the default for ${priority}).`
}

/** Tiers the wizard drops on save outside legacy mode, and what applies instead. */
export function tiersRemovedText(tiers: number, mode: 'default' | 'policy', priority: Priority, defaultPolicy: EscalationPolicy | null): string | null {
  if (tiers <= 0) return null
  const lead = `The ${tierCount(tiers)} on this rule ${tiers === 1 ? 'is' : 'are'} removed when you save`
  if (mode === 'policy') return `${lead}; the chosen policy replaces ${tiers === 1 ? 'it' : 'them'}.`
  return defaultPolicy
    ? `${lead}, so the ${priority} default policy applies.`
    : `${lead}; no policy is the default for ${priority}, so this rule will not escalate.`
}

/**
 * A chosen policy that is disabled routes nothing: the server falls back to
 * the rule's own tiers, otherwise the priority default, otherwise nothing.
 * `tiersAfterSave` is what the rule keeps (the wizard drops tiers in policy mode).
 */
export function disabledPolicyText(priority: Priority, defaultPolicy: EscalationPolicy | null, tiersAfterSave = 0): string {
  const now = tiersAfterSave > 0 ? `its ${tierCount(tiersAfterSave)}` : defaultPolicy ? `the ${priority} default policy (${defaultPolicy.name})` : 'no escalation'
  return `Disabled: until it is enabled, this rule falls back to its own legacy tiers, otherwise the ${priority} default policy, otherwise no escalation — here: ${now}.`
}

/** One line for the wizard's Review step and the rule list. */
export function ruleEscalationSummary(r: {
  policy: EscalationPolicy | null; policyId?: string | null; tiers: number[]; priority: Priority; defaultPolicy: EscalationPolicy | null
}): string {
  const source = ruleRouteSource({ policyId: r.policyId ?? r.policy?.id, policyEnabled: r.policy?.enabled, tiers: r.tiers.length, defaultPolicy: r.defaultPolicy })
  const tiers = `${tierCount(r.tiers.length)} (${r.tiers.map((m) => `${m}m`).join(' → ')})`
  const disabled = r.policy && !r.policy.enabled ? `Policy ${r.policy.name} is disabled — ` : ''
  switch (source) {
    case 'rule': return `Policy: ${r.policy?.name ?? 'chosen on the rule'}`
    case 'legacy_levels': return `${disabled}${tiers}${r.defaultPolicy ? `, taking precedence over the ${r.priority} default (${r.defaultPolicy.name})` : ''}`
    case 'priority_default': return `${disabled}${r.priority} default: ${r.defaultPolicy!.name}`
    default: return `${disabled}None (no default policy for ${r.priority})`
  }
}

/** One line for a policy list row: "3 steps · 0 → 5 min → 15 min". */
export function stepSummary(steps: Array<{ delay_minutes: number }>): string {
  if (!steps.length) return 'No steps'
  const delays = [...steps].map((s) => s.delay_minutes).sort((a, b) => a - b)
  return `${steps.length} step${steps.length === 1 ? '' : 's'} · ${delays.map((m) => (m === 0 ? '0' : fmtMinutes(m))).join(' → ')}`
}

/* ── Preview / route ──────────────────────────────────────────────────── */

function recipientFromApi(raw: unknown): PreviewRecipient | null {
  if (!isObj(raw)) return null
  return {
    kind: str(raw.kind) || 'email',
    address: strOrNull(raw.address),
    channel_id: strOrNull(raw.channel_id),
    user_id: strOrNull(raw.user_id),
    user_name: strOrNull(raw.user_name),
    via: strOrNull(raw.via),
    notified_at: strOrNull(raw.notified_at),
  }
}

function previewStepFromApi(raw: unknown, i: number): PreviewStep {
  const r = isObj(raw) ? raw : {}
  return {
    position: intOr(r.position, i),
    at_offset_minutes: Math.max(0, intOr(r.at_offset_minutes ?? r.delay_minutes, 0)),
    name: strOrNull(r.name),
    recipients: asList(r.recipients).map(recipientFromApi).filter(Boolean) as PreviewRecipient[],
    gaps: asList(r.gaps).map(str).filter(Boolean),
    notified_at: strOrNull(r.notified_at),
    current: r.current === true,
    uses_fallback: r.uses_fallback === true,
    fallback_recipients: asList(r.fallback_recipients).map(recipientFromApi).filter(Boolean) as PreviewRecipient[],
  }
}

export function previewFromApi(raw: unknown): PolicyPreview {
  const r = isObj(raw) ? raw : {}
  return {
    steps: asList(r.steps).map(previewStepFromApi).sort((a, b) => a.at_offset_minutes - b.at_offset_minutes || a.position - b.position),
    review: r.review ?? null,
    repeat: isObj(r.repeat) ? { every_minutes: minutesOrNull(r.repeat.every_minutes), max_repeats: intOr(r.repeat.max_repeats, 0) } : null,
    ack_expiry_minutes: minutesOrNull(r.ack_expiry_minutes),
    fallback: asList(r.fallback).map(recipientFromApi).filter(Boolean) as PreviewRecipient[],
    fallback_gaps: asList(r.fallback_gaps).map(str).filter(Boolean),
    warnings: asList(r.warnings).map(str).filter(Boolean),
  }
}

/** "via On-call: Primary (schedule)" etc. */
export function viaLabel(via: string | null | undefined): string {
  if (!via) return ''
  if (via === 'user') return 'Direct'
  const [kind, ...rest] = via.split(':')
  const name = rest.join(':')
  if (kind === 'schedule') return `On call: ${name}`
  if (kind === 'channel') return `Channel: ${name}`
  if (kind === 'fallback') return `Fallback${name ? `: ${name}` : ''}`
  return via
}

export function recipientLabel(r: PreviewRecipient): string {
  if (!r.address) {
    if (r.via?.startsWith('channel:')) return r.via.slice('channel:'.length) || 'Channel'
    return r.user_name || 'Unknown recipient'
  }
  return r.user_name ? `${r.user_name} <${r.address}>` : r.address
}

/** The channel name of a channel recipient (its `via` is "channel:<name>"). */
export function isChannelRecipient(r: PreviewRecipient): boolean {
  return !r.address && !!r.via?.startsWith('channel:')
}

/** Review text from the preview's free-form review field. */
export function reviewText(review: PolicyPreview['review']): string | null {
  if (!review) return null
  if (typeof review === 'string') return review
  if (review.text) return review.text
  if (review.every_minutes) {
    const who = typeof review.recipients === 'string' ? review.recipients.toLowerCase() : 'the owner and the last notified step'
    return `Unresolved updates every ${fmtMinutes(review.every_minutes)} to ${who}, also after acknowledgement.`
  }
  return null
}

export const ROUTE_SOURCE_LABEL: Record<RouteSource, string> = {
  rule: 'Chosen on the alert rule',
  priority_default: 'Default policy for this priority',
  legacy_levels: 'Legacy escalation tiers on the rule',
  none: 'No escalation',
}

export function routeFromApi(raw: unknown): RouteExplanation {
  const r = isObj(raw) ? raw : {}
  const p = isObj(r.policy) ? r.policy : null
  const src = str(p?.source ?? r.source) as RouteSource
  const st = isObj(r.state) ? r.state : null
  return {
    policy: p || r.source ? { id: strOrNull(p?.id), name: strOrNull(p?.name), source: (['rule', 'priority_default', 'legacy_levels', 'none'] as const).includes(src as RouteSource) ? src : 'none' } : null,
    priority: normalizePriority(r.priority),
    state: st ? {
      // -1 (policy) = nothing escalated yet; legacy levels count from 1 (0 = none).
      step: st.step == null || intOr(st.step, -1) < 0 ? null : intOr(st.step, 0),
      repeats: Math.max(0, intOr(st.repeats, 0)),
      acknowledged: st.acknowledged === true,
      next_at: strOrNull(st.next_at),
      next_action: strOrNull(st.next_action),
    } : null,
    steps: asList(r.steps).map(previewStepFromApi).sort((a, b) => a.at_offset_minutes - b.at_offset_minutes || a.position - b.position),
    warnings: [...asList(r.notes), ...asList(r.warnings)].map(str).filter(Boolean),
    review: r.review ?? null,
    repeat: isObj(r.repeat) ? { every_minutes: minutesOrNull(r.repeat.every_minutes), max_repeats: intOr(r.repeat.max_repeats, 0) } : null,
    fallback: asList(r.fallback).map(recipientFromApi).filter(Boolean) as PreviewRecipient[],
  }
}

/** "step:1" -> "Step 2 (Backup on-call)", "review:3" -> "Unresolved update 3", "ack_expiry", "repeat:0", "level:2". */
export function nextActionLabel(action: string | null | undefined, steps: PreviewStep[] = []): string {
  if (!action) return 'Next action'
  const [kind, raw] = action.split(':')
  const n = Number(raw)
  const stepName = (i: number) => {
    const s = steps.find((x) => x.position === i) ?? steps[i]
    return s?.name ? ` (${s.name})` : ''
  }
  if (kind === 'step' && Number.isInteger(n)) return `Step ${n + 1}${stepName(n)}`
  if (kind === 'repeat' && Number.isInteger(n)) return `Repeat of step ${n + 1}${stepName(n)}`
  if (kind === 'review') return `Unresolved-incident update${Number.isInteger(n) ? ` ${n}` : ''}`
  if (kind === 'ack_expiry') return 'Acknowledgement expiry (re-opens escalation)'
  if (kind === 'level' && Number.isInteger(n)) return `Escalation level ${n}`
  return action.replace(/_/g, ' ')
}

/** The step the incident is at: the server's flag, else the state's position. */
export function currentStep(route: RouteExplanation): PreviewStep | null {
  return route.steps.find((s) => s.current) ?? (route.state?.step != null ? route.steps.find((s) => s.position === route.state!.step) ?? null : null)
}

/** What happens next, in one sentence. `fmt` formats an instant for the viewer. */
export function nextEscalationText(route: RouteExplanation, fmt: (iso: string) => string): string {
  const src = route.policy?.source ?? 'none'
  if (src === 'none') return 'This incident has no escalation route: only the rule’s own channels are notified.'
  const st = route.state
  if (!st) return 'Escalation state is not recorded yet.'
  const action = st.next_action ? nextActionLabel(st.next_action, route.steps) : (st.acknowledged ? 'Next unresolved update' : 'Next escalation step')
  if (!st.next_at) {
    return st.acknowledged ? 'Acknowledged — step escalation stopped. No further update is scheduled.' : 'No further escalation is scheduled.'
  }
  return `${action.charAt(0).toUpperCase()}${action.slice(1)} at ${fmt(st.next_at)}${st.acknowledged ? ' (acknowledged: step escalation stopped)' : ''}.`
}

/* ── Acknowledge link (?ack=1) ────────────────────────────────────────── */

/** True when the address asks for the acknowledgement confirmation (email "Acknowledge" button). */
export function ackRequested(search: string | URLSearchParams): boolean {
  const p = typeof search === 'string' ? new URLSearchParams(search.startsWith('?') ? search.slice(1) : search) : search
  const v = (p.get('ack') || '').trim().toLowerCase()
  return v === '1' || v === 'true' || v === 'yes'
}

export type AckDialogState = 'confirm' | 'already' | 'closed' | 'pending' | 'forbidden'

/**
 * What the confirmation dialog shows. Opening it never acknowledges: only the
 * dialog's explicit confirm button sends the POST (email scanners follow GETs).
 */
export function ackDialogState(alert: { status: string } | null | undefined, canAcknowledge: boolean): AckDialogState {
  if (!canAcknowledge) return 'forbidden'
  const s = alert?.status
  if (s === 'acknowledged') return 'already'
  if (s === 'resolved') return 'closed'
  if (s === 'pending') return 'pending'
  return 'confirm'
}

/** The address without ?ack (so a refresh or back-navigation never re-opens the prompt). */
export function withoutAck(search: string): string {
  const p = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search)
  p.delete('ack')
  const s = p.toString()
  return s ? `?${s}` : ''
}

/* ── Notification holds / grouping ────────────────────────────────────── */

export function holdsFromApi(raw: unknown): NotificationHold[] {
  return asList(raw).filter(isObj).map((h) => ({
    digest_id: strOrNull(h.digest_id),
    reason: str(h.reason) || 'held',
    release_condition: str(h.release_condition),
    release_at: strOrNull(h.release_at),
    status: str(h.status) || 'open',
  }))
}

export const GROUP_BY_OPTIONS: Array<{ value: GroupBy; label: string; hint: string }> = [
  { value: 'appliance', label: 'Appliance', hint: 'One summary per ZenPlus appliance' },
  { value: 'site', label: 'Site', hint: 'Events from the same site together' },
  { value: 'device_group', label: 'Device group', hint: 'Events from the same device group' },
  { value: 'scope_tag', label: 'Responsibility scope', hint: 'Events with the same scope tag (team)' },
  { value: 'event_family', label: 'Event family', hint: 'Memory, CPU, interface errors… kept apart' },
]

export const RECOMMENDED_GROUPING: GroupingSettings = {
  enabled: true,
  group_by: ['site', 'event_family'],
  group_wait_seconds: 60,
  group_interval_seconds: 300,
  max_items: 50,
  budget_per_recipient_per_hour: 30,
  critical_reserve: true,
}

const GROUP_BY_VALUES = GROUP_BY_OPTIONS.map((o) => o.value)

export function groupingFromApi(raw: unknown): { settings: GroupingSettings; recommended: GroupingSettings } {
  const r = isObj(raw) ? raw : {}
  const src = isObj(r.settings) ? r.settings : r
  const read = (o: Record<string, any>, base: GroupingSettings): GroupingSettings => ({
    enabled: o.enabled === true,
    group_by: asList(o.group_by).map(str).filter((g): g is GroupBy => (GROUP_BY_VALUES as string[]).includes(g)),
    group_wait_seconds: Math.max(0, intOr(o.group_wait_seconds, base.group_wait_seconds)),
    group_interval_seconds: Math.max(0, intOr(o.group_interval_seconds, base.group_interval_seconds)),
    max_items: Math.max(1, intOr(o.max_items, base.max_items)),
    // null = no budget (0 in the form).
    budget_per_recipient_per_hour: o.budget_per_recipient_per_hour === null ? 0 : Math.max(0, intOr(o.budget_per_recipient_per_hour, base.budget_per_recipient_per_hour)),
    critical_reserve: o.critical_reserve !== false,
  })
  const recommended = isObj(r.recommended) ? { ...read(r.recommended, RECOMMENDED_GROUPING), enabled: true } : RECOMMENDED_GROUPING
  const settings = read(src, { ...RECOMMENDED_GROUPING, enabled: false })
  if (!isObj(src) || !Array.isArray(src.group_by)) settings.group_by = [...recommended.group_by]
  return { settings, recommended }
}

export function validateGrouping(g: GroupingSettings): Partial<Record<keyof GroupingSettings, string>> {
  const e: Partial<Record<keyof GroupingSettings, string>> = {}
  if (g.enabled && !g.group_by.length) e.group_by = 'Choose at least one grouping key'
  if (!Number.isInteger(g.group_wait_seconds) || g.group_wait_seconds < 0 || g.group_wait_seconds > 3600) e.group_wait_seconds = '0 to 3600 seconds'
  if (!Number.isInteger(g.group_interval_seconds) || g.group_interval_seconds < 30 || g.group_interval_seconds > 86400) e.group_interval_seconds = '30 to 86400 seconds'
  if (!Number.isInteger(g.max_items) || g.max_items < 5 || g.max_items > 500) e.max_items = '5 to 500 items'
  if (!Number.isInteger(g.budget_per_recipient_per_hour) || g.budget_per_recipient_per_hour < 0 || g.budget_per_recipient_per_hour > 10000) e.budget_per_recipient_per_hour = '0 (no budget) to 10000 per hour'
  return e
}

/** Plain-language summary of the grouping policy. */
export function groupingSentence(g: GroupingSettings): string {
  if (!g.enabled) return 'Grouping is off: every warning and information notification is sent on its own.'
  const keys = g.group_by.map((k) => GROUP_BY_OPTIONS.find((o) => o.value === k)?.label.toLowerCase() ?? k).join(' and ')
  const budget = g.budget_per_recipient_per_hour > 0
    ? ` A recipient who has received ${g.budget_per_recipient_per_hour} non-critical messages in the last hour gets further ones in a digest instead.`
    : ''
  return `P3/P4 trigger notifications for the same ${keys || 'group'} wait ${fmtSeconds(g.group_wait_seconds)} and go out as one summary (up to ${g.max_items} incidents); later events within ${fmtSeconds(g.group_interval_seconds)} join it.${budget} P1/P2, recoveries, escalations and reviews are never held.`
}

export const DIGEST_STATUS: Record<string, { label: string; tone: 'info' | 'success' | 'danger' | 'warning' | 'muted' }> = {
  open: { label: 'Holding', tone: 'info' },
  sending: { label: 'Sending', tone: 'warning' },
  sent: { label: 'Sent', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
  cancelled: { label: 'Cancelled', tone: 'muted' },
}

export const HOLD_REASON_LABEL: Record<string, string> = {
  grouping: 'Grouped with related warnings',
  group_wait: 'Grouped with related warnings',
  grouped: 'Grouped with related warnings',
  group: 'Grouped with related warnings',
  budget: 'Recipient message budget reached',
}

export function holdReasonLabel(reason: string): string {
  return HOLD_REASON_LABEL[reason] ?? (reason ? reason.charAt(0).toUpperCase() + reason.slice(1).replace(/_/g, ' ') : 'Held')
}

function digestFromRaw(r: Record<string, any>): Digest {
  return {
    id: str(r.id),
    group_key: str(r.group_key),
    group_label: strOrNull(r.group_label),
    channel_name: strOrNull(r.channel_name),
    recipient: strOrNull(r.recipient),
    status: str(r.status) || 'open',
    reason: str(r.reason),
    release_condition: str(r.release_condition),
    opened_at: strOrNull(r.opened_at),
    release_at: strOrNull(r.release_at),
    sent_at: strOrNull(r.sent_at),
    item_count: Math.max(0, intOr(r.item_count, asList(r.items).length)),
    error: strOrNull(r.status_detail ?? r.error),
  }
}

export function digestsFromApi(raw: unknown): Digest[] {
  return asList(raw).filter(isObj).map(digestFromRaw)
}

export function digestDetailFromApi(raw: unknown): DigestDetail {
  const r = isObj(raw) ? (isObj(raw.digest) ? { ...raw.digest, items: raw.items ?? raw.digest.items } : raw) : {}
  const items: DigestItem[] = asList(r.items).filter(isObj).map((it) => ({
    id: str(it.id) || `${str(it.alert_id)}-${str(it.added_at)}`,
    alert_id: strOrNull(it.alert_id),
    event: str(it.event) || 'trigger',
    severity: strOrNull(it.severity),
    summary: isObj(it.summary) ? it.summary : {},
    added_at: strOrNull(it.added_at),
    alert_status: strOrNull(it.alert_status ?? it.status),
    message: strOrNull(it.message ?? (isObj(it.summary) ? it.summary.headline ?? it.summary.message ?? it.summary.subject : null))
      ?? ([strOrNull(it.asset), strOrNull(it.condition)].filter(Boolean).join(' — ') || null),
    already_recovered: it.already_recovered === true || str(it.alert_status) === 'resolved',
    asset: strOrNull(it.asset),
    condition: strOrNull(it.condition),
    value: it.value == null ? null : str(it.value),
    incident_ref: strOrNull(it.incident_ref),
    priority: normalizePriority(it.priority),
  }))
  return { ...digestFromRaw(r), items }
}

/* ── Schedules ────────────────────────────────────────────────────────── */

export const ROTATION_PRESETS: Array<{ value: RotationPreset; label: string; hours: number | null }> = [
  { value: 'daily', label: 'Daily (24 h)', hours: 24 },
  { value: 'weekly', label: 'Weekly (7 days)', hours: 168 },
  { value: 'custom', label: 'Custom hours', hours: null },
]

export function rotationFor(hours: number): RotationPreset {
  return hours === 24 ? 'daily' : hours === 168 ? 'weekly' : 'custom'
}

export function rotationLabel(hours: number): string {
  if (hours === 24) return 'Daily rotation'
  if (hours === 168) return 'Weekly rotation'
  return `Every ${fmtMinutes(hours * 60)}`
}

/** Weekday indexes as stored in restrict_to (0 = Monday … 6 = Sunday, ISO order). */
export const WEEKDAYS = [
  { n: 0, short: 'Mon' }, { n: 1, short: 'Tue' }, { n: 2, short: 'Wed' }, { n: 3, short: 'Thu' },
  { n: 4, short: 'Fri' }, { n: 5, short: 'Sat' }, { n: 6, short: 'Sun' },
]

export function restrictionText(r: Restriction): string {
  const days = [...r.days].sort((a, b) => a - b)
  const dayText = days.length === 7 || !days.length ? 'Every day'
    : days.join(',') === '0,1,2,3,4' ? 'Mon–Fri'
      : days.join(',') === '5,6' ? 'Sat–Sun'
        : days.map((d) => WEEKDAYS[d]?.short ?? `#${d}`).join(', ')
  return `${dayText} ${r.start}–${r.end}`
}

function restrictionFromApi(raw: unknown): Restriction | null {
  if (!isObj(raw)) return null
  const days = asList(raw.days).map((d) => intOr(d, -1)).filter((d) => d >= 0 && d <= 6)
  const start = str(raw.start) || '09:00'
  const end = str(raw.end) || '17:00'
  return { days: [...new Set(days)].sort((a, b) => a - b), start, end }
}

function layerFromApi(raw: unknown, i: number): OncallLayer {
  const r = isObj(raw) ? raw : {}
  return {
    id: strOrNull(r.id) ?? undefined,
    name: str(r.name) || `Layer ${i + 1}`,
    position: intOr(r.position, i),
    shift_hours: Math.max(1, intOr(r.shift_hours, 168)),
    handoff_at: str(r.handoff_at),
    members: asList(r.members).map((m) => (isObj(m) ? str(m.id ?? m.user_id) : str(m))).filter(Boolean),
    restrict_to: asList(r.restrict_to).map(restrictionFromApi).filter(Boolean) as Restriction[],
  }
}

function overrideFromApi(raw: unknown): OncallOverride | null {
  if (!isObj(raw)) return null
  return {
    id: str(raw.id),
    user_id: str(raw.user_id),
    user_name: strOrNull(raw.user_name),
    starts_at: str(raw.starts_at ?? raw.start),
    ends_at: str(raw.ends_at ?? raw.end),
    reason: strOrNull(raw.reason),
  }
}

export function scheduleFromApi(raw: unknown): OncallSchedule {
  const r = isObj(raw) ? raw : {}
  return {
    id: str(r.id),
    name: str(r.name) || 'Untitled schedule',
    description: strOrNull(r.description),
    timezone: str(r.timezone) || 'UTC',
    enabled: r.enabled !== false,
    layers: asList(r.layers).map(layerFromApi).sort((a, b) => a.position - b.position),
    overrides: asList(r.overrides).map(overrideFromApi).filter(Boolean) as OncallOverride[],
    now: isObj(r.now) ? onCallNowFromApi(r.now) : null,
    used_by: asList(r.used_by).map(str).filter(Boolean),
  }
}

export function memberCount(s: OncallSchedule): number {
  return new Set(s.layers.flatMap((l) => l.members)).size
}

/* Time-zone arithmetic (a wall time in an IANA zone <-> a UTC instant). */

function validZone(tz: string): string {
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: tz })
    return tz
  } catch {
    return 'UTC'
  }
}

function zoneOffsetMs(utcMs: number, tz: string): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: validZone(tz), hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric',
  }).formatToParts(new Date(utcMs))
  const p: Record<string, number> = {}
  for (const x of parts) if (x.type !== 'literal') p[x.type] = Number(x.value)
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour % 24, p.minute, p.second)
  return asUtc - Math.floor(utcMs / 1000) * 1000
}

/** "2026-10-19" + "09:00" in Asia/Riyadh -> "2026-10-19T06:00:00.000Z". */
export function zonedWallToUtc(date: string, time: string, tz: string): string | null {
  const dm = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date.trim())
  const tm = /^(\d{1,2}):(\d{2})$/.exec(time.trim())
  if (!dm || !tm) return null
  const guess = Date.UTC(+dm[1], +dm[2] - 1, +dm[3], +tm[1], +tm[2])
  if (!Number.isFinite(guess)) return null
  const off1 = zoneOffsetMs(guess, tz)
  let t = guess - off1
  const off2 = zoneOffsetMs(t, tz)
  if (off2 !== off1) t = guess - off2
  return new Date(t).toISOString()
}

/** A UTC instant as its wall date/time in a zone. */
export function utcToZonedWall(iso: string | number, tz: string): { date: string; time: string } {
  const ms = typeof iso === 'number' ? iso : Date.parse(iso)
  if (!Number.isFinite(ms)) return { date: '', time: '' }
  const local = new Date(ms + zoneOffsetMs(ms, tz))
  const pad = (n: number) => String(n).padStart(2, '0')
  return {
    date: `${local.getUTCFullYear()}-${pad(local.getUTCMonth() + 1)}-${pad(local.getUTCDate())}`,
    time: `${pad(local.getUTCHours())}:${pad(local.getUTCMinutes())}`,
  }
}

export function emptyScheduleDraft(tz: string, now: number = Date.now()): ScheduleDraft {
  const wall = utcToZonedWall(now, tz)
  return {
    name: '',
    description: '',
    timezone: tz,
    enabled: true,
    layers: [{ key: nextKey('l'), name: 'Primary', rotation: 'weekly', shift_hours: '168', handoff_date: wall.date, handoff_time: '09:00', members: [], restrict_to: [] }],
  }
}

export function draftFromSchedule(s: OncallSchedule): ScheduleDraft {
  return {
    name: s.name,
    description: s.description ?? '',
    timezone: s.timezone,
    enabled: s.enabled,
    layers: s.layers.map((l) => {
      const wall = l.handoff_at ? utcToZonedWall(l.handoff_at, s.timezone) : { date: '', time: '09:00' }
      return {
        key: nextKey('l'), id: l.id, name: l.name, rotation: rotationFor(l.shift_hours), shift_hours: String(l.shift_hours),
        handoff_date: wall.date, handoff_time: wall.time, members: [...l.members], restrict_to: l.restrict_to.map((r) => ({ ...r, days: [...r.days] })),
      }
    }),
  }
}

export function newLayer(d: ScheduleDraft, now: number = Date.now()) {
  const wall = utcToZonedWall(now, d.timezone)
  return { key: nextKey('l'), name: `Layer ${d.layers.length + 1}`, rotation: 'weekly' as RotationPreset, shift_hours: '168', handoff_date: wall.date, handoff_time: '09:00', members: [] as string[], restrict_to: [] as Restriction[] }
}

/** Layers keep their order: the last one listed has the highest position and wins overlaps. */
export function schedulePayload(d: ScheduleDraft) {
  return {
    name: d.name.trim(),
    description: d.description.trim() || null,
    timezone: d.timezone,
    enabled: d.enabled,
    layers: d.layers.map((l, i) => ({
      ...(l.id ? { id: l.id } : {}),
      name: l.name.trim() || `Layer ${i + 1}`,
      position: i,
      shift_hours: Math.round(Number(l.shift_hours)),
      handoff_at: zonedWallToUtc(l.handoff_date, l.handoff_time, d.timezone),
      members: [...l.members],
      restrict_to: l.restrict_to.map((r) => ({ days: [...r.days].sort((a, b) => a - b), start: r.start, end: r.end })),
    })),
  }
}

export function scheduleDirty(a: ScheduleDraft, b: ScheduleDraft): boolean {
  return JSON.stringify(schedulePayload(a)) !== JSON.stringify(schedulePayload(b))
}

export type ScheduleErrors = { name?: string; timezone?: string; layers?: string; layerErrors?: Record<string, string> }

export function validateSchedule(d: ScheduleDraft): ScheduleErrors {
  const e: ScheduleErrors = {}
  if (!d.name.trim()) e.name = 'Name the schedule'
  if (validZone(d.timezone) !== d.timezone) e.timezone = 'Choose a valid time zone'
  if (!d.layers.length) e.layers = 'Add at least one layer'
  const le: Record<string, string> = {}
  for (const l of d.layers) {
    const h = Number(l.shift_hours)
    if (!Number.isInteger(h) || h < 1 || h > 8760) le[l.key] = 'Shift length: whole hours from 1 to 8760'
    else if (!zonedWallToUtc(l.handoff_date, l.handoff_time, d.timezone)) le[l.key] = 'Set the handoff date and time'
    else if (!l.members.length) le[l.key] = 'Add at least one member to the rotation'
    else if (l.restrict_to.some((r) => !r.days.length || !/^\d{2}:\d{2}$/.test(r.start) || !/^\d{2}:\d{2}$/.test(r.end) || r.start === r.end)) {
      le[l.key] = 'Each restriction needs days and a start and end time that differ'
    }
  }
  if (Object.keys(le).length) e.layerErrors = le
  return e
}

export const hasScheduleErrors = (e: ScheduleErrors) => !!(e.name || e.timezone || e.layers || (e.layerErrors && Object.keys(e.layerErrors).length))

/** Move an item up (-1) or down (+1); out-of-range moves return the list unchanged. */
export function moveItem<T>(list: T[], index: number, dir: -1 | 1): T[] {
  const to = index + dir
  if (index < 0 || index >= list.length || to < 0 || to >= list.length) return list
  const next = [...list]
  const [x] = next.splice(index, 1)
  next.splice(to, 0, x)
  return next
}

/** Who takes each upcoming shift of a layer: the rotation order from the handoff. */
export function rotationOrder(members: string[], shiftHours: number, handoffIso: string | null, now: number, count = 4): Array<{ user_id: string; start: number; end: number }> {
  if (!members.length || !handoffIso || !(shiftHours > 0)) return []
  const t0 = Date.parse(handoffIso)
  if (!Number.isFinite(t0)) return []
  const len = shiftHours * 3600_000
  const idx = Math.floor((now - t0) / len)
  const out: Array<{ user_id: string; start: number; end: number }> = []
  for (let i = 0; i < count; i++) {
    const k = idx + i
    const start = t0 + k * len
    out.push({ user_id: members[((k % members.length) + members.length) % members.length], start, end: start + len })
  }
  return out
}

/* Timeline */

export function shiftsFromApi(raw: unknown): Shift[] {
  return asList(raw, 'shifts').filter(isObj).map((s) => ({
    start: str(s.start),
    end: str(s.end),
    user_id: strOrNull(s.user_id),
    user_name: strOrNull(s.user_name),
    layer: strOrNull(s.layer),
    override: s.override === true,
  })).filter((s) => Number.isFinite(Date.parse(s.start)) && Number.isFinite(Date.parse(s.end)) && Date.parse(s.end) > Date.parse(s.start))
}

export type TimelineBar = { key: string; leftPct: number; widthPct: number; user_id: string | null; user_name: string; start: string; end: string; override: boolean; colorIndex: number }
export type TimelineRow = { key: string; label: string; bars: TimelineBar[] }

export const USER_COLORS = 8

/** Stable colour slot for a user (FNV-1a with a final mix, so similar UUIDs spread out). */
export function colorIndex(id: string | null | undefined): number {
  const s = id || ''
  if (!s) return USER_COLORS - 1
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0 }
  h ^= h >>> 16
  h = Math.imul(h, 0x85ebca6b) >>> 0
  h ^= h >>> 13
  return h % USER_COLORS
}

/** Place shifts on a [from, to) window: one row per layer, overrides on their own row first. */
export function layoutShifts(shifts: Shift[], from: number, to: number): TimelineRow[] {
  const span = to - from
  if (!(span > 0)) return []
  // Everyone on this timeline gets a distinct colour (sorted ids, so it is stable between refreshes).
  const ids = [...new Set(shifts.map((s) => s.user_id ?? ''))].sort()
  const colourOf = (id: string | null) => (ids.length <= USER_COLORS ? ids.indexOf(id ?? '') : colorIndex(id))
  const rows = new Map<string, TimelineRow>()
  const rowFor = (s: Shift) => {
    const key = s.override ? '__override' : `layer:${s.layer ?? ''}`
    if (!rows.has(key)) rows.set(key, { key, label: s.override ? 'Overrides' : s.layer || 'Rotation', bars: [] })
    return rows.get(key)!
  }
  for (const s of shifts) {
    const a = Math.max(from, Date.parse(s.start))
    const b = Math.min(to, Date.parse(s.end))
    if (!(b > a)) continue
    const row = rowFor(s)
    row.bars.push({
      key: `${row.key}-${s.start}-${s.user_id}`,
      leftPct: ((a - from) / span) * 100,
      widthPct: ((b - a) / span) * 100,
      user_id: s.user_id,
      user_name: s.user_name || 'Nobody',
      start: s.start,
      end: s.end,
      override: s.override,
      colorIndex: colourOf(s.user_id),
    })
  }
  const list = [...rows.values()]
  for (const r of list) r.bars.sort((x, y) => x.leftPct - y.leftPct)
  return list.sort((x, y) => (x.key === '__override' ? -1 : y.key === '__override' ? 1 : 0))
}

/** Gaps in coverage inside the window (no shift in any row), merged. */
export function coverageGapsInWindow(shifts: Shift[], from: number, to: number): Array<{ start: number; end: number }> {
  const spans = shifts
    .map((s) => [Math.max(from, Date.parse(s.start)), Math.min(to, Date.parse(s.end))] as const)
    .filter(([a, b]) => b > a)
    .sort((x, y) => x[0] - y[0])
  const gaps: Array<{ start: number; end: number }> = []
  let cursor = from
  for (const [a, b] of spans) {
    if (a > cursor) gaps.push({ start: cursor, end: a })
    cursor = Math.max(cursor, b)
  }
  if (cursor < to) gaps.push({ start: cursor, end: to })
  return gaps
}

/** Start of the viewer's local day and a `days`-long window. */
export function timelineWindow(now: number, days = 14): { from: number; to: number } {
  const d = new Date(now)
  d.setHours(0, 0, 0, 0)
  const from = d.getTime()
  const end = new Date(from)
  end.setDate(end.getDate() + days)
  return { from, to: end.getTime() }
}

export function dayTicks(from: number, days: number): Array<{ at: number; leftPct: number }> {
  const out: Array<{ at: number; leftPct: number }> = []
  const end = new Date(from)
  end.setDate(end.getDate() + days)
  const span = end.getTime() - from
  for (let i = 0; i < days; i++) {
    const d = new Date(from)
    d.setDate(d.getDate() + i)
    out.push({ at: d.getTime(), leftPct: ((d.getTime() - from) / span) * 100 })
  }
  return out
}

export function onCallNowFromApi(raw: unknown): OnCallNow {
  const r = isObj(raw) ? raw : {}
  const users = asList(r.users).map((u) => {
    if (typeof u === 'string') return { id: u, name: u }
    if (!isObj(u)) return null
    const id = str(u.id ?? u.user_id)
    return { id, name: str(u.name ?? u.user_name ?? u.full_name ?? u.username) || id }
  }).filter(Boolean) as OnCallNow['users']
  return { users, until: strOrNull(r.until) }
}

/* ── Coverage ─────────────────────────────────────────────────────────── */

function refFromApi(raw: unknown): NamedRef | null {
  if (typeof raw === 'string') return { id: raw, name: raw }
  if (!isObj(raw)) return null
  const id = strOrNull(raw.id ?? raw.user_id ?? raw.schedule_id)
  return { id, name: str(raw.name ?? raw.user_name ?? raw.username ?? raw.full_name) || 'Unknown user', reason: strOrNull(raw.reason) }
}

export function coverageFromApi(raw: unknown): Coverage {
  const r = isObj(raw) ? raw : {}
  const pr = isObj(r.priorities) ? r.priorities : {}
  const priorities: Coverage['priorities'] = {}
  for (const p of PRIORITIES) {
    const e = pr[p]
    if (!isObj(e)) continue
    priorities[p] = {
      policy_id: strOrNull(e.policy_id),
      policy_name: strOrNull(e.policy_name),
      steps_ok: e.steps_ok === true,
      gaps: asList(e.gaps).map(str).filter(Boolean),
    } as CoverageEntry
  }
  return {
    priorities,
    schedules_without_members: asList(r.schedules_without_members).map(refFromApi).filter(Boolean) as NamedRef[],
    users_without_contacts: asList(r.users_without_contacts).map(refFromApi).filter(Boolean) as NamedRef[],
    unrouted_priorities: asList(r.unrouted_priorities).map(normalizePriority).filter(Boolean) as Priority[],
    schedule_gaps: asList(r.schedule_gaps).filter(isObj).map((g) => ({
      schedule_id: strOrNull(g.schedule_id), schedule_name: str(g.schedule_name) || 'Schedule', start: str(g.start), end: str(g.end),
    })),
  }
}

export type CoverageIssue = { key: string; severity: 'danger' | 'warning'; text: string; fix: { label: string; tab: string; id?: string | null } }

/** Every coverage problem as an actionable line, worst first. */
export function coverageIssues(c: Coverage, fmt: (iso: string) => string = (x) => x): CoverageIssue[] {
  const out: CoverageIssue[] = []
  for (const p of c.unrouted_priorities) {
    // P4 is information only: no paging escalation is the recommended design.
    if (p === 'P4') continue
    out.push({ key: `unrouted-${p}`, severity: isUrgent(p) ? 'danger' : 'warning', text: `${PRIORITY_META[p].label} incidents have no escalation policy: a rule without its own policy or tiers notifies only its channels.`, fix: { label: 'Assign a policy', tab: 'policies' } })
  }
  for (const p of PRIORITIES) {
    const e = c.priorities[p]
    // An unrouted priority's only "gap" is that it is unrouted (listed above).
    if (!e || c.unrouted_priorities.includes(p)) continue
    for (const g of e.gaps) out.push({ key: `gap-${p}-${g}`, severity: isUrgent(p) ? 'danger' : 'warning', text: `${PRIORITY_META[p].label} · ${e.policy_name ?? 'policy'}: ${g}`, fix: { label: 'Edit policy', tab: 'policies', id: e.policy_id } })
  }
  for (const s of c.schedules_without_members) {
    out.push({ key: `sched-${s.id ?? s.name}`, severity: 'warning', text: `On-call schedule “${s.name}” has no members, so it resolves to nobody.`, fix: { label: 'Add members', tab: 'oncall', id: s.id } })
  }
  for (const g of c.schedule_gaps) {
    out.push({ key: `sgap-${g.schedule_id}-${g.start}`, severity: 'warning', text: `Nobody is on call in “${g.schedule_name}” from ${fmt(g.start)} to ${fmt(g.end)}.`, fix: { label: 'Fix schedule', tab: 'oncall', id: g.schedule_id } })
  }
  for (const u of c.users_without_contacts) {
    const why = u.reason && u.reason !== 'No email or SMS contact method' ? ` (${u.reason.toLowerCase()})` : ''
    out.push({ key: `user-${u.id ?? u.name}`, severity: 'warning', text: `${u.name}${why} cannot be reached: pages to them are not delivered until they have an enabled email or SMS contact method.`, fix: { label: 'Contact methods', tab: 'profile', id: u.id } })
  }
  return out.sort((a, b) => (a.severity === b.severity ? 0 : a.severity === 'danger' ? -1 : 1))
}

export type CoverageStatus = { tone: 'success' | 'warning' | 'danger' | 'muted'; label: string }

/** One status for a priority card. */
export function priorityCoverageStatus(p: Priority, c: Coverage | null | undefined, policy: EscalationPolicy | null): CoverageStatus {
  if (c?.unrouted_priorities.includes(p) || (!policy && !c?.priorities[p]?.policy_id)) {
    return p === 'P4' ? { tone: 'muted', label: 'No paging (by design)' } : { tone: isUrgent(p) ? 'danger' : 'warning', label: 'Unrouted' }
  }
  const e = c?.priorities[p]
  if (!e) return { tone: 'muted', label: 'Coverage not checked' }
  if (e.gaps.length || !e.steps_ok) return { tone: isUrgent(p) ? 'danger' : 'warning', label: `${e.gaps.length || 1} gap${e.gaps.length > 1 ? 's' : ''}` }
  return { tone: 'success', label: 'Covered' }
}

/* ── Recommended matrix ───────────────────────────────────────────────── */

export type RecommendedForm = {
  primary_schedule_id: string
  backup_schedule_id: string
  lead_user_id: string
  noc_channel_ids: string[]
  team_channel_ids: string[]
  /** Move existing P1..P4 defaults to the new policies (otherwise the server refuses with 409). */
  replace_defaults: boolean
}

export function recommendedPayload(f: RecommendedForm) {
  return {
    replace_defaults: f.replace_defaults,
    primary_schedule_id: f.primary_schedule_id || null,
    backup_schedule_id: f.backup_schedule_id || null,
    lead_user_id: f.lead_user_id || null,
    noc_channel_ids: [...f.noc_channel_ids],
    team_channel_ids: [...f.team_channel_ids],
  }
}

export function validateRecommended(f: RecommendedForm): string | null {
  if (!f.primary_schedule_id && !f.noc_channel_ids.length && !f.team_channel_ids.length) return 'Choose the primary on-call schedule or at least one NOC or team channel'
  if (f.backup_schedule_id && f.backup_schedule_id === f.primary_schedule_id) return 'The backup schedule must differ from the primary'
  return null
}

/**
 * What the recommended matrix creates (mirrors escalation_policies._recommended:
 * steps without a target are left out).
 */
export function recommendedPlan(f: RecommendedForm, names: (t: Target) => string): Array<{ priority: Priority; ack: number | null; review: number | null; steps: Array<{ delay: number; who: string }> }> {
  const list = (...xs: string[]) => xs.filter(Boolean).join(' + ')
  const primary = f.primary_schedule_id ? names({ type: 'schedule', id: f.primary_schedule_id }) : ''
  const backup = f.backup_schedule_id ? names({ type: 'schedule', id: f.backup_schedule_id }) : ''
  const lead = f.lead_user_id ? names({ type: 'user', id: f.lead_user_id }) : ''
  const noc = f.noc_channel_ids.map((id) => names({ type: 'channel', id })).join(', ')
  const team = f.team_channel_ids.map((id) => names({ type: 'channel', id })).join(', ')
  const steps = (...items: Array<[number, string]>) => items.filter(([, who]) => who).map(([delay, who]) => ({ delay, who }))
  return [
    { priority: 'P1', ack: 5, review: 15, steps: steps([0, list(primary, noc)], [5, backup], [15, lead], [30, list(noc, team, lead) && `${list(noc, team, lead)} (major incident)`]) },
    { priority: 'P2', ack: 15, review: 30, steps: steps([0, list(primary, team, noc)], [15, backup], [30, lead]) },
    { priority: 'P3', ack: 60, review: null, steps: steps([0, team || primary], [240, lead]) },
    { priority: 'P4', ack: null, review: null, steps: [] },
  ]
}

/* ── Contact methods ──────────────────────────────────────────────────── */

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export function validateContact(kind: ContactKind, address: string): string | null {
  const a = address.trim()
  if (!a) return kind === 'email' ? 'Enter an email address' : 'Enter a phone number'
  if (a.length > 255) return 'At most 255 characters'
  if (kind === 'email') return EMAIL_RE.test(a) ? null : 'Not a valid email address'
  const digits = a.replace(/[\s().-]/g, '')
  return /^\+?\d{6,20}$/.test(digits) ? null : 'Use international format, e.g. +966 50 123 4567'
}

export function contactsFromApi(raw: unknown): ContactMethod[] {
  return asList(raw, 'methods').filter(isObj).map((m) => ({
    id: strOrNull(m.id) ?? undefined,
    kind: (m.kind === 'sms' ? 'sms' : 'email') as ContactKind,
    address: str(m.address),
    label: strOrNull(m.label),
    enabled: m.enabled !== false,
  }))
}

/** Same kind and address (emails case-insensitive, phones by digits) = the same method. */
export function sameContact(a: { kind: ContactKind; address: string }, b: { kind: ContactKind; address: string }): boolean {
  if (a.kind !== b.kind) return false
  if (a.kind === 'email') return a.address.trim().toLowerCase() === b.address.trim().toLowerCase()
  return a.address.replace(/\D/g, '') === b.address.replace(/\D/g, '')
}

export function contactsPayload(list: ContactMethod[]) {
  return list.map((m) => ({ ...(m.id ? { id: m.id } : {}), kind: m.kind, address: m.address.trim(), label: m.label?.trim() || null, enabled: m.enabled }))
}

/* ── Page tabs ────────────────────────────────────────────────────────── */

export const ESCALATION_TABS = ['matrix', 'policies', 'oncall', 'targets', 'grouping', 'coverage'] as const
export type EscalationTab = (typeof ESCALATION_TABS)[number]

export function tabFromSearch(search: string | URLSearchParams): EscalationTab {
  const p = typeof search === 'string' ? new URLSearchParams(search.startsWith('?') ? search.slice(1) : search) : search
  const t = p.get('tab') as EscalationTab | null
  return t && (ESCALATION_TABS as readonly string[]).includes(t) ? t : 'matrix'
}
