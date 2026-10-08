/**
 * Report viewer URL contract: the period (`range`, `from`, `to`) and scope
 * (`nodes`, `app`, `nf`, `custom_id`) of /reports/view/:key.
 *
 *   range = PeriodId | custom | legacy 1h / 24h / 7d / 1M / month / last_month / quarter
 *   no range → the template's default period, else last_24h
 *
 * Calendar periods and custom ranges are interpreted in the organisation
 * timezone (Settings → General → Company), never the browser's.
 */
import { isPeriodId, PERIOD_LABEL, periodRange, zonedToUtc } from '../periods'
import type { CatalogTemplate, PeriodId, ScopeFilter, SectionDef, TemplateEntry } from '../types'

export type PeriodSelection =
  | { kind: 'period'; id: PeriodId }
  /** Legacy `range=1h` links (no PeriodId equivalent). */
  | { kind: 'hour' }
  | { kind: 'custom'; from: Date; to: Date }

const LEGACY: Record<string, PeriodId> = {
  '24h': 'last_24h', '7d': 'last_7d', '30d': 'last_30d', '1M': 'last_30d', '90d': 'last_90d',
  month: 'month_to_date', last_month: 'previous_month', quarter: 'quarter_to_date',
}

export const MAX_WINDOW_DAYS = 400

function validDate(s: string | null | undefined): Date | null {
  if (!s) return null
  const d = new Date(s)
  return Number.isFinite(d.getTime()) ? d : null
}

/** Parse the URL period; anything unusable falls back to the template default. */
export function parseSelection(range: string | null, from: string | null, to: string | null,
  fallback: PeriodId = 'last_24h'): PeriodSelection {
  if (range && isPeriodId(range)) return { kind: 'period', id: range }
  if (range && LEGACY[range]) return { kind: 'period', id: LEGACY[range] }
  if (range === '1h') return { kind: 'hour' }
  const f = validDate(from)
  const t = validDate(to)
  if ((range === 'custom' || !range) && f && t && f < t) return { kind: 'custom', from: f, to: t }
  return { kind: 'period', id: fallback }
}

/** True when the URL names no usable period, so the template default applies. */
export function needsDefaultPeriod(range: string | null, from: string | null, to: string | null): boolean {
  if (range && (isPeriodId(range) || LEGACY[range] || range === '1h')) return false
  if (range && range !== 'custom') return true
  const f = validDate(from)
  const t = validDate(to)
  return !(f && t && f < t)
}

/** URL parameters for a selection (relative periods carry no dates). */
export function selectionParams(sel: PeriodSelection): { range: string; from?: string; to?: string } {
  if (sel.kind === 'period') return { range: sel.id }
  if (sel.kind === 'hour') return { range: '1h' }
  return { range: 'custom', from: sel.from.toISOString(), to: sel.to.toISOString() }
}

/** [from, to) of a selection; `now` should be bucketed so renders are cached. */
export function resolveWindow(sel: PeriodSelection, tz: string, weekStart: string, now: Date): { from: Date; to: Date } {
  if (sel.kind === 'custom') return { from: sel.from, to: sel.to }
  if (sel.kind === 'hour') return { from: new Date(now.getTime() - 3_600_000), to: now }
  return periodRange(sel.id, tz, weekStart, now)
}

export function selectionKey(sel: PeriodSelection): string {
  return sel.kind === 'custom' ? `custom:${sel.from.getTime()}-${sel.to.getTime()}` : sel.kind === 'hour' ? 'hour' : sel.id
}

export function selectionLabel(sel: PeriodSelection): string {
  if (sel.kind === 'period') return PERIOD_LABEL[sel.id] ?? sel.id
  if (sel.kind === 'hour') return 'Last hour'
  return 'Custom range'
}

/* ------------------------------------------------------------------ */
/*  Custom range inputs (datetime-local) in the organisation timezone  */
/* ------------------------------------------------------------------ */

const pad = (n: number) => String(n).padStart(2, '0')

/** `YYYY-MM-DDTHH:mm` wall time of an instant in `tz` (for a datetime-local input). */
export function toZonedInput(d: Date, tz: string): string {
  let fmt: Intl.DateTimeFormat
  try {
    fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', hourCycle: 'h23',
    })
  } catch {
    return toZonedInput(d, 'UTC')
  }
  const p: Record<string, number> = {}
  for (const part of fmt.formatToParts(d)) if (part.type !== 'literal') p[part.type] = Number(part.value)
  return `${p.year}-${pad(p.month)}-${pad(p.day)}T${pad(p.hour % 24)}:${pad(p.minute)}`
}

/** The instant a `YYYY-MM-DDTHH:mm` wall time denotes in `tz`; null when malformed. */
export function fromZonedInput(value: string, tz: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value || '')
  if (!m) return null
  const [y, mo, d, h, mi] = m.slice(1).map(Number)
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59) return null
  const out = zonedToUtc(y, mo, d, tz, h, mi)
  return Number.isFinite(out.getTime()) ? out : null
}

/** Validation message for a custom range, or null when it can be rendered. */
export function customRangeError(from: Date | null, to: Date | null, now = new Date()): string | null {
  if (!from || !to) return 'Enter both a start and an end.'
  if (from >= to) return 'The start must be before the end.'
  if (from > now) return 'The start is in the future.'
  if (to.getTime() - from.getTime() > MAX_WINDOW_DAYS * 86_400_000) return `A report covers at most ${MAX_WINDOW_DAYS} days.`
  return null
}

/* ------------------------------------------------------------------ */
/*  Scope filters a template honours                                   */
/* ------------------------------------------------------------------ */

/**
 * Which scope pickers a report offers. Built-in templates declare
 * `filterable`; organisation templates derive it from their sections (as the
 * server's report_templates.filterable_for does).
 */
export function filterableFor(template: Pick<CatalogTemplate, 'filterable'> | { entries?: TemplateEntry[]; sections?: string[] } | null | undefined,
  sections: Pick<SectionDef, 'id' | 'scope'>[] | undefined): ScopeFilter[] {
  if (!template) return []
  if ('filterable' in template && Array.isArray(template.filterable) && template.filterable.length) return template.filterable
  const ids = 'entries' in template && template.entries?.length
    ? template.entries.map((e) => e.id)
    : ('sections' in template && Array.isArray(template.sections) ? template.sections : [])
  const scopeOf = new Map((sections || []).map((s) => [s.id, s.scope]))
  const out: ScopeFilter[] = []
  if (ids.some((id) => scopeOf.get(id) === 'device')) out.push('devices')
  if (ids.some((id) => id.startsWith('rum_'))) out.push('applications')
  if (ids.some((id) => scopeOf.get(id) === 'netflow')) out.push('netflow')
  return out
}

/** Comma-separated device ids from `nodes=` (deduplicated, blanks dropped). */
export function parseNodes(param: string | null): string[] {
  return [...new Set((param || '').split(',').map((s) => s.trim()).filter(Boolean))]
}
