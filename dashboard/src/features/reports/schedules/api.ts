/**
 * Report schedule data hooks. Query keys:
 *   ['report-schedules']               the schedule list (Reports → Schedules, Channels → Routing)
 *   ['report-schedules', id, 'runs']   one schedule's run history
 *   ['report-schedules', 'status']     scheduler heartbeat, overdue count, share-link availability
 *   ['reports', 'runs', 'page', …]     Delivered reports (GET /reports/runs, paged)
 *   ['reports', 'settings']            link expiry, retention, attachment size, retries
 *   ['settings', 'channels']           notification channels, normalised to a plain array
 *                                      (shared with ChannelsPage / AlertRules — keep the shape)
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type {
  NotificationChannel, ReportSchedule, ReportSettings, RunsPage, ScheduleBody, ScheduleRun, ScheduleStatus,
} from './model'

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

export const SCHEDULE_RUNS_LIMIT = 20

export function useScheduleRuns(id: string | null | undefined) {
  return useQuery<{ runs: ScheduleRun[]; canViewLinks: boolean | null; total: number | null }>({
    queryKey: ['report-schedules', id, 'runs'],
    queryFn: async () => {
      const r = (await api.get(`/report-schedules/${id}/runs`, { params: { limit: SCHEDULE_RUNS_LIMIT } })).data
      return {
        runs: asList<ScheduleRun>(r),
        canViewLinks: typeof r?.can_view_links === 'boolean' ? r.can_view_links : null,
        total: typeof r?.total === 'number' ? r.total : null,
      }
    },
    enabled: !!id,
    // A running run or a pending retry changes on its own.
    refetchInterval: (q) => (q.state.data?.runs.some((x) => x.status === 'running' || x.next_retry_at) ? 15_000 : false),
  })
}

export const RUNS_PAGE = 50

/** Delivered reports, newest first, 50 per page; optional status and schedule filters. */
export function useRunsPages(filters: { status?: string | null; scheduleId?: string | null }) {
  return useInfiniteQuery<RunsPage>({
    queryKey: ['reports', 'runs', 'page', filters.status ?? '', filters.scheduleId ?? ''],
    initialPageParam: 0,
    queryFn: async ({ pageParam }) => {
      const params: Record<string, string | number> = { limit: RUNS_PAGE, offset: Number(pageParam) || 0 }
      if (filters.status) params.status = filters.status
      if (filters.scheduleId) params.schedule_id = filters.scheduleId
      const r = (await api.get('/reports/runs', { params })).data
      // A bare array is the pre-1.24.23 reply.
      if (Array.isArray(r)) return { data: r as ScheduleRun[], total: r.length, limit: RUNS_PAGE, offset: 0 }
      return {
        data: asList<ScheduleRun>(r),
        total: typeof r?.total === 'number' ? r.total : 0,
        limit: typeof r?.limit === 'number' ? r.limit : RUNS_PAGE,
        offset: typeof r?.offset === 'number' ? r.offset : Number(pageParam) || 0,
      }
    },
    getNextPageParam: (last) => {
      const next = last.offset + last.data.length
      return last.data.length > 0 && next < last.total ? next : undefined
    },
  })
}

/** Scheduler heartbeat, overdue schedules and whether report emails can carry a link. */
export function useScheduleStatus(opts?: { enabled?: boolean }) {
  return useQuery<ScheduleStatus>({
    queryKey: ['report-schedules', 'status'],
    queryFn: async () => (await api.get('/report-schedules/status')).data,
    enabled: opts?.enabled ?? true,
    staleTime: 30_000,
    refetchInterval: 60_000,
  })
}

export function useReportSettings(opts?: { enabled?: boolean }) {
  return useQuery<ReportSettings>({
    queryKey: ['reports', 'settings'],
    // {settings, limits}; a bare object is accepted too.
    queryFn: async () => {
      const r = (await api.get('/reports/settings')).data
      return (r?.settings ?? r) as ReportSettings
    },
    enabled: opts?.enabled ?? true,
    staleTime: 60_000,
  })
}

export function useSaveReportSettings() {
  const qc = useQueryClient()
  return useMutation<ReportSettings, unknown, ReportSettings>({
    mutationFn: async (body) => {
      const r = (await api.put('/reports/settings', body)).data
      return (r?.settings ?? r) as ReportSettings
    },
    onSuccess: (data) => {
      if (data) qc.setQueryData(['reports', 'settings'], data)
      qc.invalidateQueries({ queryKey: ['reports', 'settings'] })
      qc.invalidateQueries({ queryKey: ['report-schedules', 'status'] })
    },
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
    qc.invalidateQueries({ queryKey: ['report-schedules', 'status'] })
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

/** Partial update, e.g. {hour, minute} for "Run at 08:00 instead". */
export function usePatchSchedule() {
  const invalidate = useInvalidate()
  return useMutation<ReportSchedule, unknown, { id: string; body: Partial<ScheduleBody> }>({
    mutationFn: async ({ id, body }) => (await api.put(`/report-schedules/${id}`, body)).data,
    onSuccess: invalidate,
  })
}

/** Switch a classic schedule to its replacement template, keeping everything else. */
export function useConvertSchedule() {
  const invalidate = useInvalidate()
  return useMutation<ReportSchedule, unknown, string>({
    mutationFn: async (id) => (await api.post(`/report-schedules/${id}/convert`)).data,
    onSuccess: invalidate,
  })
}

/** Expire a run's share link now. */
export function useRevokeRun() {
  const invalidate = useInvalidate()
  return useMutation<unknown, unknown, string>({
    mutationFn: async (id) => (await api.post(`/reports/runs/${id}/revoke`)).data,
    onSettled: invalidate,
  })
}

/** Resend a run to its remaining failed targets now. */
export function useRetryRun() {
  const invalidate = useInvalidate()
  return useMutation<{ status?: string; delivered?: string[]; errors?: string[]; remaining?: number } | undefined, unknown, string>({
    // {result: {status, delivered, errors, remaining}, run}
    mutationFn: async (id) => {
      const r = (await api.post(`/reports/runs/${id}/retry`, undefined, { timeout: 120_000 })).data
      return r?.result ?? r
    },
    onSettled: invalidate,
  })
}

/** Download a run's stored attachment with the session's credentials; returns the file name. */
export async function downloadRunArtifact(id: string, fallbackName?: string | null): Promise<string> {
  const res = await api.get(`/reports/runs/${id}/artifact`, { responseType: 'blob', timeout: 120_000 })
  const cd = String(res.headers['content-disposition'] || '')
  const star = /filename\*=UTF-8''([^;]+)/i.exec(cd)
  const plain = /filename="?([^";]+)"?/i.exec(cd)
  const name = star ? decodeURIComponent(star[1]) : plain ? plain[1] : fallbackName || 'report'
  const url = URL.createObjectURL(res.data as Blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
  return name
}

export function useDeleteSchedule() {
  const invalidate = useInvalidate()
  return useMutation<unknown, unknown, string>({
    mutationFn: async (id) => (await api.delete(`/report-schedules/${id}`)).data,
    onSuccess: invalidate,
  })
}
