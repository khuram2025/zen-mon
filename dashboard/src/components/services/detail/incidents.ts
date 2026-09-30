/**
 * Incident reconstruction (B14, B15).
 *
 * Preferred source: GET /service-checks/{id}/incidents, built on the server
 * from the full status log including the state entering the window. On an
 * older appliance the browser rebuilds incidents from status history with
 * the same rules:
 *   - down → Down incident; warning/degraded → Warning incident
 *   - a change of kind (Warning → Down) closes one interval and opens the next
 *   - unknown / paused / anything else is an evidence gap: it closes the
 *     incident at that transition and is never drawn as a warning
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { withCapability } from './capabilities'
import type { IncidentsResponse, Outage, ServerIncident, StatusHistoryEvent } from './types'

export type SeededEvent = StatusHistoryEvent & { seeded?: boolean }

type Kind = 'down' | 'warn' | 'up' | 'gap'

export function statusKind(status: string | null | undefined): Kind {
  if (status === 'down') return 'down'
  if (status === 'warning' || status === 'degraded') return 'warn'
  if (status === 'up') return 'up'
  return 'gap'
}

/**
 * Prepend the last transition before `fromTs` (the entering state) as a
 * synthetic event at `fromTs`, so a window that opens mid-outage carries it.
 */
export function seedHistoryWithPrior(
  history: StatusHistoryEvent[],
  prior: StatusHistoryEvent | null | undefined,
  fromTs: number,
): SeededEvent[] {
  if (history.some((h) => Date.parse(h.timestamp) <= fromTs)) return history
  if (!prior) return history
  const t = Date.parse(prior.timestamp)
  if (!Number.isFinite(t) || t >= fromTs) return history
  return [{ timestamp: new Date(fromTs).toISOString(), old_status: prior.old_status, new_status: prior.new_status, reason: prior.reason, seeded: true }, ...history]
}

/** The newest event strictly before `ts` from any list (used when a range-specific prior query is absent). */
export function lastEventBefore(events: StatusHistoryEvent[], ts: number): StatusHistoryEvent | null {
  let best: StatusHistoryEvent | null = null
  let bestT = -Infinity
  for (const e of events) {
    const t = Date.parse(e.timestamp)
    if (Number.isFinite(t) && t < ts && t > bestT) { best = e; bestT = t }
  }
  return best
}

export function buildOutages(
  history: SeededEvent[],
  fromTs: number,
  toTs: number,
  createdAt: string | null | undefined,
  nowMs: number,
): Outage[] {
  const sorted = [...history]
    .filter((h) => Number.isFinite(Date.parse(h.timestamp)))
    .sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp))

  // An incident can only start once the check exists: clamp seeded/inferred starts.
  const createdTs = createdAt ? Date.parse(createdAt) : NaN
  const clampStart = (t: number) => (Number.isFinite(createdTs) ? Math.max(t, createdTs) : t)

  const out: Outage[] = []
  let open: { start: number; kind: 'down' | 'warn'; reason?: string; clipped: boolean; path: string[] } | null = null

  for (const ev of sorted) {
    const t = Date.parse(ev.timestamp)
    const k = statusKind(ev.new_status)
    if (open && k === open.kind) continue
    let path: string[] = []
    if (open) {
      const endKind: Outage['endKind'] = k === 'up' ? 'recovered' : k === 'gap' ? 'gap' : 'escalated'
      path = open.path
      out.push({
        id: `c-${open.start}-${open.kind}`,
        start: open.start,
        end: t,
        kind: open.kind,
        reason: open.reason,
        reasonEnd: ev.reason || undefined,
        clippedStart: open.clipped,
        endKind,
        severityPath: open.path,
      })
      open = null
    }
    if (k === 'down' || k === 'warn') {
      open = {
        start: clampStart(t),
        kind: k,
        reason: ev.reason || undefined,
        clipped: ev.seeded === true,
        path: [...path, k === 'down' ? 'down' : 'warning'],
      }
    }
  }
  if (open) {
    out.push({
      id: `c-${open.start}-${open.kind}`,
      start: open.start, end: null, kind: open.kind, reason: open.reason,
      clippedStart: open.clipped, endKind: 'ongoing', severityPath: open.path,
    })
  }

  // A historical window's trailing incident is not "ongoing" — its end lies beyond the window.
  const windowReachesNow = toTs >= nowMs - 60_000
  const clipped = out.map((o) => {
    if (o.end == null) {
      if (windowReachesNow) return o
      return { ...o, end: Math.min(toTs, nowMs), clippedEnd: true, endKind: 'window' as const }
    }
    if (o.end > toTs) return { ...o, end: toTs, clippedEnd: true, endKind: 'window' as const }
    return o
  })
  return clipped
    .filter((o) => (o.end ?? nowMs) > fromTs && o.start < toTs && (o.end ?? nowMs) > o.start)
    .sort((a, b) => b.start - a.start)
}

/** Chart shading: down/warning intervals; unknown is a gap, never a warning (B15). */
export function statusBands(history: SeededEvent[], fromTs: number, toTs: number, nowMs: number) {
  const sorted = [...history].sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp))
  const out: { start: number; end: number; kind: 'down' | 'warn' }[] = []
  let open: { start: number; kind: 'down' | 'warn' } | null = null
  for (const ev of sorted) {
    const t = Date.parse(ev.timestamp)
    if (!Number.isFinite(t)) continue
    const k = statusKind(ev.new_status)
    if (open && k === open.kind) continue
    if (open) {
      out.push({ start: open.start, end: t, kind: open.kind })
      open = null
    }
    if (k === 'down' || k === 'warn') open = { start: t, kind: k }
  }
  if (open) out.push({ start: open.start, end: Math.min(nowMs, toTs), kind: open.kind })
  return out.filter((b) => b.end > fromTs && b.start < toTs)
}

export function outagesFromServer(rows: ServerIncident[], toTs: number, nowMs: number): Outage[] {
  const windowReachesNow = toTs >= nowMs - 60_000
  return rows.map((r) => {
    const start = Date.parse(r.start)
    const end = r.end ? Date.parse(r.end) : null
    return {
      id: r.id,
      start,
      startISO: r.start,
      end,
      kind: r.kind === 'down' ? 'down' : 'warn',
      reason: r.reason_start || undefined,
      reasonEnd: r.reason_end || undefined,
      clippedStart: !!r.starts_before_window,
      clippedEnd: end != null ? end > toTs : !windowReachesNow,
      endKind: r.ongoing ? (windowReachesNow ? 'ongoing' : 'window') : end != null && end > toTs ? 'window' : 'recovered',
      severityPath: r.severity_path,
      durationSec: r.duration_sec,
    } satisfies Outage
  })
}

export type IncidentsResult =
  | { supported: true; outages: Outage[]; total: number; response: IncidentsResponse }
  | { supported: false }

export function useServerIncidents(
  checkId: string,
  fromISO: string,
  toISO: string,
  limit: number,
  nowMs: number,
  enabled: boolean,
) {
  const toTs = Date.parse(toISO)
  return useQuery<IncidentsResult>({
    queryKey: ['service-incidents', checkId, fromISO, toISO, limit],
    queryFn: async () => {
      const resp = await withCapability('incidents', async () =>
        (await api.get<IncidentsResponse>(`/service-checks/${checkId}/incidents`, {
          params: { from: fromISO, to: toISO, skip: 0, limit },
        })).data)
      if (!resp || !Array.isArray(resp.data)) return { supported: false }
      return {
        supported: true,
        outages: outagesFromServer(resp.data, toTs, nowMs).sort((a, b) => b.start - a.start),
        total: resp.meta?.total ?? resp.data.length,
        response: resp,
      }
    },
    enabled,
    retry: (n, e: any) => n < 1 && e?.response?.status >= 500,
    refetchInterval: 60_000,
    // Keep rows visible while "Load more" widens the page, never across range changes.
    placeholderData: (prev, prevQuery) =>
      prevQuery?.queryKey[2] === fromISO && prevQuery?.queryKey[3] === toISO ? prev : undefined,
  })
}
