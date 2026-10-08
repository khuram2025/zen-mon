/**
 * Report periods: rolling windows and calendar windows in the organisation
 * timezone (mirrors report_scheduler._resolve_period on the server).
 */
import { isValidTimezone } from '@/lib/branding'
import type { PeriodId } from './types'

export const PERIODS: { id: PeriodId; label: string; short: string; kind: 'rolling' | 'calendar' }[] = [
  { id: 'last_24h', label: 'Last 24 hours', short: '24h', kind: 'rolling' },
  { id: 'last_7d', label: 'Last 7 days', short: '7d', kind: 'rolling' },
  { id: 'last_30d', label: 'Last 30 days', short: '30d', kind: 'rolling' },
  { id: 'last_90d', label: 'Last 90 days', short: '90d', kind: 'rolling' },
  { id: 'today', label: 'Today', short: 'Today', kind: 'calendar' },
  { id: 'yesterday', label: 'Yesterday', short: 'Yesterday', kind: 'calendar' },
  { id: 'week_to_date', label: 'This week', short: 'This week', kind: 'calendar' },
  { id: 'previous_week', label: 'Previous week', short: 'Last week', kind: 'calendar' },
  { id: 'month_to_date', label: 'This month', short: 'This month', kind: 'calendar' },
  { id: 'previous_month', label: 'Previous month', short: 'Last month', kind: 'calendar' },
  { id: 'quarter_to_date', label: 'This quarter', short: 'This quarter', kind: 'calendar' },
  { id: 'previous_quarter', label: 'Previous quarter', short: 'Last quarter', kind: 'calendar' },
]

export const PERIOD_LABEL: Record<string, string> = Object.fromEntries(PERIODS.map((p) => [p.id, p.label]))

export function isPeriodId(v: string | null | undefined): v is PeriodId {
  return !!v && PERIODS.some((p) => p.id === v)
}

type Wall = { y: number; m: number; d: number; h: number; mi: number; s: number; dow: number }

function wall(at: Date, tz: string): Wall {
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: isValidTimezone(tz) ? tz : 'UTC',
    year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric',
    second: 'numeric', hourCycle: 'h23', weekday: 'short',
  })
  const p: Record<string, string> = {}
  for (const part of fmt.formatToParts(at)) p[part.type] = part.value
  const dow = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].indexOf(p.weekday)
  return { y: +p.year, m: +p.month, d: +p.day, h: +p.hour % 24, mi: +p.minute, s: +p.second, dow }
}

/** The UTC instant of a wall-clock time in ``tz`` (handles DST by re-checking). */
export function zonedToUtc(y: number, m: number, d: number, tz: string, h = 0, mi = 0): Date {
  let guess = Date.UTC(y, m - 1, d, h, mi)
  for (let i = 0; i < 2; i++) {
    const w = wall(new Date(guess), tz)
    const asUtc = Date.UTC(w.y, w.m - 1, w.d, w.h, w.mi, w.s)
    guess += Date.UTC(y, m - 1, d, h, mi) - asUtc
  }
  return new Date(guess)
}

const WEEK_START: Record<string, number> = { sunday: 0, monday: 1, saturday: 6 }

/** [from, to) for a period, in the organisation timezone. */
export function periodRange(id: PeriodId, tz: string, weekStart = 'monday', now = new Date()): { from: Date; to: Date } {
  const H = 3600_000
  switch (id) {
    case 'last_24h': return { from: new Date(now.getTime() - 24 * H), to: now }
    case 'last_7d': return { from: new Date(now.getTime() - 7 * 24 * H), to: now }
    case 'last_30d': return { from: new Date(now.getTime() - 30 * 24 * H), to: now }
    case 'last_90d': return { from: new Date(now.getTime() - 90 * 24 * H), to: now }
  }
  const w = wall(now, tz)
  const dayStart = (y: number, m: number, d: number) => zonedToUtc(y, m, d, tz)
  const addDays = (y: number, m: number, d: number, n: number) => {
    const t = new Date(Date.UTC(y, m - 1, d + n))
    return [t.getUTCFullYear(), t.getUTCMonth() + 1, t.getUTCDate()] as const
  }
  const today = dayStart(w.y, w.m, w.d)
  switch (id) {
    case 'today': return { from: today, to: now }
    case 'yesterday': {
      const [y, m, d] = addDays(w.y, w.m, w.d, -1)
      return { from: dayStart(y, m, d), to: today }
    }
    case 'week_to_date':
    case 'previous_week': {
      const back = (w.dow - (WEEK_START[weekStart] ?? 1) + 7) % 7
      const [y, m, d] = addDays(w.y, w.m, w.d, -back)
      const weekStartAt = dayStart(y, m, d)
      if (id === 'week_to_date') return { from: weekStartAt, to: now }
      const [py, pm, pd] = addDays(y, m, d, -7)
      return { from: dayStart(py, pm, pd), to: weekStartAt }
    }
    case 'month_to_date': return { from: dayStart(w.y, w.m, 1), to: now }
    case 'previous_month': {
      const pm = w.m === 1 ? 12 : w.m - 1
      const py = w.m === 1 ? w.y - 1 : w.y
      return { from: dayStart(py, pm, 1), to: dayStart(w.y, w.m, 1) }
    }
    case 'quarter_to_date':
    case 'previous_quarter': {
      const qStartMonth = Math.floor((w.m - 1) / 3) * 3 + 1
      const qStart = dayStart(w.y, qStartMonth, 1)
      if (id === 'quarter_to_date') return { from: qStart, to: now }
      const pm = qStartMonth === 1 ? 10 : qStartMonth - 3
      const py = qStartMonth === 1 ? w.y - 1 : w.y
      return { from: dayStart(py, pm, 1), to: qStart }
    }
  }
  return { from: new Date(now.getTime() - 7 * 24 * H), to: now }
}

/** Round a rolling "now" down to 5 minutes so cached renders are reused. */
export function bucketNow(now = new Date()): Date {
  return new Date(Math.floor(now.getTime() / 300_000) * 300_000)
}
