/**
 * Escalation & on-call data hooks (work/ESCALATION-PLAN.md "API"). Query keys:
 *   ['escalation', 'policies']                      GET /escalation-policies
 *   ['escalation', 'preview', id, priority, rev]   POST /escalation-policies/{id}/preview (dry run)
 *   ['escalation', 'schedules']                     GET /oncall/schedules
 *   ['escalation', 'schedule', id]                  GET /oncall/schedules/{id}
 *   ['escalation', 'timeline', id, from, to]        GET /oncall/schedules/{id}/timeline
 *   ['escalation', 'now', id]                       GET /oncall/schedules/{id}/now
 *   ['escalation', 'coverage']                      GET /oncall/coverage
 *   ['alerts', 'builtin-routing']                   GET/PUT /alerts/builtin-routing (built-in event families)
 *   ['alerts', 'response-targets']                  GET /alerts/response-targets
 *   ['alerts', 'notification-grouping']             GET /alerts/notification-grouping
 *   ['alerts', 'digests', status]                   GET /alerts/digests
 *   ['alerts', 'digest', id]                        GET /alerts/digests/{id}
 *   ['alerts', 'route', id]                         GET /alerts/{id}/route
 *   ['alerts', 'channel-directory']                 GET /alerts/channel-directory (shared with RoutingCard)
 *   ['users', 'me', 'contact-methods']              GET /users/me/contact-methods
 * Every response is normalized by ./model before a screen reads it.
 */
import { useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { useCan } from '@/stores/auth'
import { useAssignees } from '@/components/alerts/AlertBits'
import type { AlertChannelRef } from '@/components/alerts/types'
import {
  asList, builtinRoutingFromApi, builtinRoutingPayload, contactsFromApi, coverageFromApi, digestDetailFromApi, digestsFromApi, groupingFromApi, memberCount,
  onCallNowFromApi, policyFromApi, previewFromApi, routeFromApi, scheduleFromApi, shiftsFromApi, targetsFromApi,
  type Directory,
} from './model'
import type { EscalationPolicy, OncallSchedule, Priority } from './types'

export function usePolicies(enabled = true) {
  return useQuery<EscalationPolicy[]>({
    queryKey: ['escalation', 'policies'],
    queryFn: async () => asList((await api.get('/escalation-policies')).data).map(policyFromApi),
    enabled,
    staleTime: 30_000,
  })
}

export function usePolicyPreview(id: string | null, priority: Priority, revision: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: ['escalation', 'preview', id, priority, revision ?? ''],
    queryFn: async () => previewFromApi((await api.post(`/escalation-policies/${id}/preview`, { priority })).data),
    enabled: enabled && !!id,
    staleTime: 30_000,
    retry: false,
  })
}

/**
 * Dry run of an unsaved draft (POST /escalation-policies/dry-run): the editor's
 * live panel. `body` is the policyPayload of the draft; null = do not run
 * (invalid draft). Debounced by the caller through the key.
 */
export function useDraftDryRun(body: unknown | null, priority: Priority, enabled = true) {
  const key = body == null ? null : JSON.stringify(body)
  return useQuery({
    queryKey: ['escalation', 'dry-run', priority, key],
    queryFn: async () => previewFromApi((await api.post('/escalation-policies/dry-run', { policy: body, priority })).data),
    enabled: enabled && key != null,
    staleTime: 30_000,
    retry: false,
    placeholderData: (prev) => prev,
  })
}

export function useSchedules(enabled = true) {
  return useQuery<OncallSchedule[]>({
    queryKey: ['escalation', 'schedules'],
    queryFn: async () => asList((await api.get('/oncall/schedules')).data).map(scheduleFromApi),
    enabled,
    staleTime: 30_000,
  })
}

export function useSchedule(id: string | null) {
  return useQuery<OncallSchedule>({
    queryKey: ['escalation', 'schedule', id],
    queryFn: async () => scheduleFromApi((await api.get(`/oncall/schedules/${id}`)).data),
    enabled: !!id,
  })
}

export function useScheduleTimeline(id: string | null, from: number, to: number) {
  const fromIso = new Date(from).toISOString()
  const toIso = new Date(to).toISOString()
  return useQuery({
    queryKey: ['escalation', 'timeline', id, fromIso, toIso],
    queryFn: async () => shiftsFromApi((await api.get(`/oncall/schedules/${id}/timeline`, { params: { from: fromIso, to: toIso } })).data),
    enabled: !!id,
    staleTime: 60_000,
  })
}

export function useOnCallNow(id: string | null) {
  return useQuery({
    queryKey: ['escalation', 'now', id],
    queryFn: async () => onCallNowFromApi((await api.get(`/oncall/schedules/${id}/now`)).data),
    enabled: !!id,
    refetchInterval: 60_000,
  })
}

export function useCoverage(enabled = true) {
  return useQuery({
    queryKey: ['escalation', 'coverage'],
    queryFn: async () => coverageFromApi((await api.get('/oncall/coverage')).data),
    enabled,
    staleTime: 30_000,
  })
}

/** Which built-in event families are routed by their priority's default policy (alerts.view to read). */
export function useBuiltinRouting(enabled = true) {
  return useQuery({
    queryKey: ['alerts', 'builtin-routing'],
    queryFn: async () => builtinRoutingFromApi((await api.get('/alerts/builtin-routing')).data),
    enabled,
    staleTime: 30_000,
    retry: false,
  })
}

/** Switch one family on or off (alerts.manage); coverage is refetched afterwards. */
export function useSetBuiltinRouting() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ enabled, key, on }: { enabled: string[]; key: string; on: boolean }) =>
      builtinRoutingFromApi((await api.put('/alerts/builtin-routing', builtinRoutingPayload(enabled, key, on))).data),
    onSuccess: (data) => {
      if (data.families.length) qc.setQueryData(['alerts', 'builtin-routing'], data)
      qc.invalidateQueries({ queryKey: ['alerts', 'builtin-routing'] })
      qc.invalidateQueries({ queryKey: ['escalation', 'coverage'] })
    },
  })
}

export function useResponseTargets() {
  return useQuery({
    queryKey: ['alerts', 'response-targets'],
    queryFn: async () => targetsFromApi((await api.get('/alerts/response-targets')).data),
    staleTime: 60_000,
  })
}

export function useGrouping() {
  return useQuery({
    queryKey: ['alerts', 'notification-grouping'],
    queryFn: async () => groupingFromApi((await api.get('/alerts/notification-grouping')).data),
  })
}

export function useDigests(status: string) {
  return useQuery({
    queryKey: ['alerts', 'digests', status],
    queryFn: async () => digestsFromApi((await api.get('/alerts/digests', { params: { ...(status ? { status } : {}), limit: 100 } })).data),
    refetchInterval: 30_000,
  })
}

export function useDigest(id: string | null) {
  return useQuery({
    queryKey: ['alerts', 'digest', id],
    queryFn: async () => digestDetailFromApi((await api.get(`/alerts/digests/${id}`)).data),
    enabled: !!id,
  })
}

export function useAlertRoute(alertId: string, enabled = true) {
  return useQuery({
    queryKey: ['alerts', 'route', alertId],
    queryFn: async () => routeFromApi((await api.get(`/alerts/${alertId}/route`)).data),
    enabled: enabled && !!alertId,
    refetchInterval: 30_000,
    retry: false,
  })
}

/** Channel names for pickers. The directory needs alerts.manage; read-only roles get none. */
export function useChannelDirectory(enabled = true) {
  return useQuery<{ data: AlertChannelRef[] }>({
    queryKey: ['alerts', 'channel-directory'],
    queryFn: async () => (await api.get('/alerts/channel-directory')).data,
    enabled,
    staleTime: 5 * 60_000,
  })
}

export function useMyContactMethods() {
  return useQuery({
    queryKey: ['users', 'me', 'contact-methods'],
    queryFn: async () => {
      const data = (await api.get('/users/me/contact-methods')).data
      return {
        methods: contactsFromApi(data),
        timezone: typeof data?.timezone === 'string' && data.timezone ? data.timezone as string : null,
        account_email: typeof data?.account_email === 'string' ? data.account_email as string : null,
        implicit_email: data?.implicit_email !== false,
      }
    },
  })
}

/** Schedules, users and channels by id, for target labels and pickers. */
export function useDirectory(enabled = true): Directory & { loading: boolean } {
  const can = useCan()
  const schedules = useSchedules(enabled)
  const users = useAssignees(enabled)
  const channels = useChannelDirectory(enabled && can('alerts.manage'))
  return useMemo(() => ({
    schedules: (schedules.data ?? []).map((s) => ({ id: s.id, name: s.name, enabled: s.enabled, members: memberCount(s) })),
    users: (users.data ?? []).map((u) => ({ id: u.id, name: u.name, username: u.username })),
    channels: (channels.data?.data ?? []).map((c) => ({ id: c.id, name: c.name, type: c.type, enabled: c.enabled })),
    loading: schedules.isLoading || users.isLoading,
  }), [schedules.data, users.data, channels.data, schedules.isLoading, users.isLoading])
}

/** The organisation time zone (report branding, readable with reports.view), else the browser's. */
export function useOrgTimezone(): string {
  const can = useCan()
  const q = useQuery<{ timezone?: string }>({
    queryKey: ['reports', 'branding'],
    queryFn: async () => (await api.get('/reports/branding')).data,
    enabled: can('reports.view'),
    staleTime: 5 * 60_000,
  })
  let browser = 'UTC'
  try { browser = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' } catch { /* old engines */ }
  return q.data?.timezone || browser
}
