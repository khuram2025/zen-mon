/**
 * Windows Event Log filters in an agent policy, edited as text: one channel per line,
 *
 *   System: critical, error, warning
 *   Security: information | 4625, 4740
 *
 * (levels after the colon, optional event IDs after "|"). An empty list means the default channels below, which
 * the appliance sends to agents (server/app/services/event_log_policy.py); collection is switched off with the
 * policy's `event_log` feature flag, never with an empty list.
 */

export type EventLogFilter = { channel: string; levels: string[]; ids?: number[] }

export const EVENT_LOG_LEVELS = ['critical', 'error', 'warning', 'information', 'verbose'] as const

export const DEFAULT_EVENT_LOG_TEXT = 'System: critical, error, warning\nApplication: critical, error, warning'

export function formatEventLogFilters(filters: ReadonlyArray<Record<string, unknown>> | null | undefined): string {
  return (filters || []).map((f) => {
    const levels = Array.isArray(f.levels) ? f.levels.join(', ') : ''
    const ids = Array.isArray(f.ids) && f.ids.length ? ` | ${f.ids.join(', ')}` : ''
    return `${String(f.channel ?? '')}: ${levels}${ids}`
  }).join('\n')
}

/** The filters in the text, or the first problem (line number and what is wrong). Blank text = []. */
export function parseEventLogFilters(text: string): { filters: EventLogFilter[] } | { error: string } {
  const filters: EventLogFilter[] = []
  const seen = new Set<string>()
  const lines = text.split('\n')
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim()
    if (!line) continue
    const n = i + 1
    const colon = line.indexOf(':')
    if (colon <= 0) return { error: `Line ${n}: write "Channel: level, level", e.g. "System: critical, error".` }
    const channel = line.slice(0, colon).trim()
    const [levelPart, idPart] = line.slice(colon + 1).split('|')
    if (seen.has(channel.toLowerCase())) return { error: `Line ${n}: ${channel} is listed twice.` }
    seen.add(channel.toLowerCase())
    const levels: string[] = []
    for (const raw of levelPart.split(',')) {
      let lv = raw.trim().toLowerCase()
      if (!lv) continue
      if (lv === 'info') lv = 'information'
      if (!(EVENT_LOG_LEVELS as readonly string[]).includes(lv)) {
        return { error: `Line ${n}: "${raw.trim()}" is not a level (${EVENT_LOG_LEVELS.join(', ')}).` }
      }
      if (!levels.includes(lv)) levels.push(lv)
    }
    if (!levels.length) return { error: `Line ${n}: ${channel} needs at least one level.` }
    const ids: number[] = []
    for (const raw of (idPart || '').split(',')) {
      const s = raw.trim()
      if (!s) continue
      const id = Number(s)
      if (!Number.isInteger(id) || id < 1) return { error: `Line ${n}: "${s}" is not an event ID.` }
      if (!ids.includes(id)) ids.push(id)
    }
    filters.push(ids.length ? { channel, levels, ids } : { channel, levels })
  }
  return { filters }
}

/** One line for the policy list: Off, Default, or the channels. */
export function eventLogSummary(policy: { event_log_filters?: ReadonlyArray<Record<string, unknown>> | null; feature_flags?: Record<string, unknown> | null }): string {
  const flags = policy.feature_flags || {}
  const flag = 'collect_event_log' in flags ? flags.collect_event_log : flags.event_log
  if (flag === false) return 'Off'
  const filters = policy.event_log_filters || []
  if (!filters.length) return 'Default (System, Application)'
  return filters.map((f) => String(f.channel)).join(', ')
}
