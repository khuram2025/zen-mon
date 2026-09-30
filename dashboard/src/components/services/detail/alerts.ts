/**
 * Alerts for one monitor (B11, B12, B25).
 *
 * - The list is bounded to the selected window and paged with meta.total.
 * - The active count is its own query (status=active, limit=1 → meta.total),
 *   so headers and "Ack all" never depend on the visible page.
 * - "Ack all" acknowledges every ACTIVE alert for this check (not just the
 *   visible ones): POST /alerts/acknowledge-bulk, or on older appliances by
 *   paging through status=active and acknowledging each.
 * - Rule coverage mirrors the alert engine's service matcher.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { toast } from '@/components/ui/Toast'
import { knownUnsupported, markUnsupported } from './capabilities'
import { isUnsupported } from './format'
import type { AlertsPage, ServiceAlert } from './types'

export type AlertStatusFilter = 'all' | 'active' | 'acknowledged' | 'resolved'

export function useWindowAlerts(checkId: string, fromISO: string, toISO: string, status: AlertStatusFilter, page: number, pageSize: number, enabled: boolean) {
  return useQuery<AlertsPage>({
    queryKey: ['service-alerts', checkId, fromISO, toISO, status, page, pageSize],
    queryFn: async () => (await api.get('/alerts', {
      params: {
        service_check_id: checkId,
        from: fromISO,
        to: toISO,
        skip: page * pageSize,
        limit: pageSize,
        ...(status !== 'all' ? { status } : {}),
      },
    })).data,
    enabled,
    refetchInterval: 30_000,
    placeholderData: (prev, prevQuery) =>
      prevQuery?.queryKey[2] === fromISO && prevQuery?.queryKey[3] === toISO && prevQuery?.queryKey[4] === status ? prev : undefined,
  })
}

/** Every active alert for this check, independent of the window and page. */
export function useActiveAlertCount(checkId: string, enabled: boolean) {
  return useQuery<number>({
    queryKey: ['service-alerts', checkId, 'active-count'],
    queryFn: async () => {
      const r = (await api.get<AlertsPage>('/alerts', { params: { service_check_id: checkId, status: 'active', limit: 1 } })).data
      return r?.meta?.total ?? r?.data?.length ?? 0
    },
    enabled,
    refetchInterval: 30_000,
  })
}

async function allActiveIds(checkId: string): Promise<string[]> {
  const ids: string[] = []
  const pageSize = 200
  for (let skip = 0; skip < 5000; skip += pageSize) {
    const r = (await api.get<AlertsPage>('/alerts', { params: { service_check_id: checkId, status: 'active', skip, limit: pageSize } })).data
    const rows = r?.data || []
    ids.push(...rows.map((a) => a.id))
    if (rows.length < pageSize || ids.length >= (r?.meta?.total ?? 0)) break
  }
  return ids
}

export function useAckAll(checkId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (): Promise<number> => {
      if (!knownUnsupported('ack-bulk')) {
        try {
          const r = (await api.post('/alerts/acknowledge-bulk', { service_check_id: checkId })).data
          return typeof r?.acknowledged === 'number' ? r.acknowledged : 0
        } catch (e) {
          if (!isUnsupported(e)) throw e
          markUnsupported('ack-bulk')
        }
      }
      const ids = await allActiveIds(checkId)
      let done = 0
      // Sequential batches keep the appliance responsive for large backlogs.
      for (let i = 0; i < ids.length; i += 10) {
        const batch = ids.slice(i, i + 10)
        const results = await Promise.allSettled(batch.map((alertId) => api.post(`/alerts/${alertId}/acknowledge`)))
        done += results.filter((x) => x.status === 'fulfilled').length
      }
      if (ids.length > 0 && done < ids.length) throw new Error(`Acknowledged ${done} of ${ids.length} active alerts; retry for the rest.`)
      return done
    },
    onSuccess: (n) => {
      toast.success(n === 0 ? 'No active alerts to acknowledge' : `Acknowledged ${n} alert${n === 1 ? '' : 's'}`)
    },
    onError: (e: any) => toast.error('Acknowledge failed', apiErrorMessage(e)),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['service-alerts', checkId] })
      qc.invalidateQueries({ queryKey: ['alerts'] })
    },
  })
}

type RuleLike = {
  enabled?: boolean
  service_check_id?: string | null
  service_check_group_id?: string | null
  metric?: string | null
}

/**
 * Same candidate + scope rules as the alert engine's service path: a rule
 * is a service rule when it names a check, a check group, or the
 * service_status metric; its check/group scopes must then match this check.
 */
export function ruleCoversCheck(r: RuleLike, checkId: string, groupId: string | null | undefined): boolean {
  if (r?.enabled === false) return false
  const sc = r?.service_check_id ? String(r.service_check_id) : null
  const grp = r?.service_check_group_id ? String(r.service_check_group_id) : null
  const candidate = !!sc || !!grp || r?.metric === 'service_status'
  if (!candidate) return false
  if (sc && sc !== checkId) return false
  if (grp && grp !== (groupId || '')) return false
  return true
}

export function useRuleCoverage(checkId: string, groupId: string | null | undefined, enabled: boolean) {
  return useQuery<{ covered: boolean; count: number }>({
    queryKey: ['service-alert-rule-coverage', checkId, groupId],
    queryFn: async () => {
      const raw = (await api.get('/alert-rules')).data
      const rules: RuleLike[] = Array.isArray(raw) ? raw : raw?.data || []
      const matches = rules.filter((r) => ruleCoversCheck(r, checkId, groupId))
      return { covered: matches.length > 0, count: matches.length }
    },
    enabled,
    staleTime: 120_000,
    retry: false,
  })
}

export function alertsInSpan(alerts: ServiceAlert[], start: number, end: number): ServiceAlert[] {
  return alerts.filter((a) => {
    const t = Date.parse(a.triggered_at)
    return Number.isFinite(t) && t >= start && t <= end
  })
}
