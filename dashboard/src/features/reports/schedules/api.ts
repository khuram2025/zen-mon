/**
 * Report schedule data hooks. Query keys:
 *   ['report-schedules']               the schedule list (Reports → Schedules, Channels → Routing)
 *   ['report-schedules', id, 'runs']   one schedule's run history
 *   ['settings', 'channels']           notification channels, normalised to a plain array
 *                                      (shared with ChannelsPage / AlertRules — keep the shape)
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { NotificationChannel, ReportSchedule, ScheduleBody, ScheduleRun } from './model'

export const SCHEDULES_KEY = ['report-schedules'] as const

function asList<T>(r: unknown): T[] {
  if (Array.isArray(r)) return r as T[]
  const d = (r as { data?: unknown } | null)?.data
  return Array.isArray(d) ? (d as T[]) : []
}

export function useReportSchedules(opts?: { enabled?: boolean }) {
  return useQuery<ReportSchedule[]>({
    queryKey: SCHEDULES_KEY,
    queryFn: async () => asList<ReportSchedule>((await api.get('/report-schedules')).data),
    enabled: opts?.enabled ?? true,
    staleTime: 15_000,
  })
}

export function useScheduleRuns(id: string | null | undefined) {
  return useQuery<{ runs: ScheduleRun[]; canViewLinks: boolean | null }>({
    queryKey: ['report-schedules', id, 'runs'],
    queryFn: async () => {
      const r = (await api.get(`/report-schedules/${id}/runs`)).data
      return {
        runs: asList<ScheduleRun>(r),
        canViewLinks: typeof r?.can_view_links === 'boolean' ? r.can_view_links : null,
      }
    },
    enabled: !!id,
  })
}

export function useNotificationChannels(opts?: { enabled?: boolean }) {
  return useQuery<NotificationChannel[]>({
    queryKey: ['settings', 'channels'],
    queryFn: async () => asList<NotificationChannel>((await api.get('/settings/channels')).data),
    enabled: opts?.enabled ?? true,
  })
}

function useInvalidate() {
  const qc = useQueryClient()
  return () => {
    qc.invalidateQueries({ queryKey: SCHEDULES_KEY })
    qc.invalidateQueries({ queryKey: ['reports', 'runs'] })
    qc.invalidateQueries({ queryKey: ['reports', 'catalog'] })
  }
}

export function useSaveSchedule() {
  const invalidate = useInvalidate()
  return useMutation<ReportSchedule, unknown, { id?: string | null; body: ScheduleBody }>({
    mutationFn: async ({ id, body }) =>
      id ? (await api.put(`/report-schedules/${id}`, body)).data : (await api.post('/report-schedules', body)).data,
    onSuccess: invalidate,
  })
}

/** Enable/disable with an optimistic switch that rolls back on failure. */
export function useToggleSchedule() {
  const qc = useQueryClient()
  return useMutation<ReportSchedule, unknown, string, { prev?: ReportSchedule[] }>({
    mutationFn: async (id) => (await api.post(`/report-schedules/${id}/toggle`)).data,
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: SCHEDULES_KEY })
      const prev = qc.getQueryData<ReportSchedule[]>(SCHEDULES_KEY)
      if (prev) {
        qc.setQueryData<ReportSchedule[]>(SCHEDULES_KEY, prev.map((s) => (s.id === id ? { ...s, enabled: !s.enabled } : s)))
      }
      return { prev }
    },
    onError: (_e, _id, ctx) => {
      if (ctx?.prev) qc.setQueryData(SCHEDULES_KEY, ctx.prev)
    },
    onSettled: () => qc.invalidateQueries({ queryKey: SCHEDULES_KEY }),
  })
}

export type RunNowResult = {
  message?: string
  status?: string
  delivered?: string[]
  errors?: string[]
  token?: string | null
  run_id?: string
}

export function useRunScheduleNow() {
  const invalidate = useInvalidate()
  return useMutation<RunNowResult, unknown, string>({
    mutationFn: async (id) => (await api.post(`/report-schedules/${id}/run-now`, undefined, { timeout: 300_000 })).data,
    onSettled: invalidate,
  })
}

export function useDeleteSchedule() {
  const invalidate = useInvalidate()
  return useMutation<unknown, unknown, string>({
    mutationFn: async (id) => (await api.delete(`/report-schedules/${id}`)).data,
    onSuccess: invalidate,
  })
}
