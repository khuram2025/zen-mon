/**
 * Reports data hooks. Query keys:
 *   ['reports', 'catalog']               catalog (library, viewer, designer, schedules)
 *   ['reports', 'branding']              organisation branding for reports
 *   ['reports', 'render', key, params]   a rendered report (viewer)
 *   ['reports', 'template', id]          one organisation template (designer)
 *   ['reports', 'runs']                  recent generated reports
 *   ['reports', 'rum-applications']      RUM applications (scope picker, schedule chips)
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { DEFAULT_BRANDING, type Branding } from '@/lib/branding'
import type {
  CustomTemplate, RenderReport, ReportCatalog, ReportRun, TemplateBody, TemplateEntry,
} from './types'

export function useReportCatalog(opts?: { enabled?: boolean }) {
  return useQuery<ReportCatalog>({
    queryKey: ['reports', 'catalog'],
    queryFn: async () => (await api.get('/reports/catalog')).data,
    enabled: opts?.enabled ?? true,
    staleTime: 60_000,
  })
}

/** Organisation branding (logo flags, colour, timezone, formats) for reports. */
export function useReportBranding() {
  const q = useQuery<Branding>({
    queryKey: ['reports', 'branding'],
    queryFn: async () => (await api.get('/reports/branding')).data,
    staleTime: 5 * 60_000,
  })
  return { ...q, branding: q.data ?? DEFAULT_BRANDING }
}

export interface RumApplication {
  application_id: string
  service_name: string
  sessions: number
  last_seen: string | null
}

/** Browser-RUM applications the caller can see; one cache entry, always an array. */
export function useRumApplications(enabled = true) {
  return useQuery<RumApplication[]>({
    queryKey: ['reports', 'rum-applications'],
    queryFn: async () => ((await api.get('/reports/rum-applications')).data?.applications ?? []) as RumApplication[],
    enabled,
    staleTime: 5 * 60_000,
  })
}

export interface RenderParams {
  key: string
  customId?: string | null
  from: string
  to: string
  deviceIds?: string[]
  applicationId?: string | null
  netflow?: string | null
}

export function renderQueryString(p: RenderParams, format = 'json'): string {
  const q = new URLSearchParams({ format, from: p.from, to: p.to })
  if (p.customId) q.set('custom_id', p.customId)
  if (p.deviceIds?.length) q.set('device_ids', p.deviceIds.join(','))
  if (p.applicationId) q.set('application_id', p.applicationId)
  if (p.netflow) q.set('netflow', p.netflow)
  return q.toString()
}

export function useRenderReport(p: RenderParams | null) {
  return useQuery<RenderReport>({
    queryKey: ['reports', 'render', p?.key, p?.customId ?? null, p?.from, p?.to,
      p?.deviceIds?.join(',') ?? '', p?.applicationId ?? '', p?.netflow ?? ''],
    queryFn: async () => (await api.get(`/reports/render/${encodeURIComponent(p!.key)}?${renderQueryString(p!)}`,
      { timeout: 180_000 })).data,
    enabled: !!p,
    placeholderData: (prev) => prev,
    staleTime: 60_000,
  })
}

/** Download a rendered report (pdf | html | xlsx); returns the file name used. */
export async function downloadReport(p: RenderParams, format: 'pdf' | 'html' | 'xlsx'): Promise<string> {
  const res = await api.get(`/reports/render/${encodeURIComponent(p.key)}?${renderQueryString(p, format)}`,
    { responseType: 'blob', timeout: 300_000 })
  const cd = String(res.headers['content-disposition'] || '')
  const star = /filename\*=UTF-8''([^;]+)/i.exec(cd)
  const plain = /filename="?([^";]+)"?/i.exec(cd)
  const name = star ? decodeURIComponent(star[1]) : plain ? plain[1] : `report.${format}`
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

export interface PreviewBody {
  name: string
  description?: string | null
  category?: string | null
  sections: TemplateEntry[]
  from: string
  to: string
  device_ids?: string[]
  application_id?: string | null
  netflow?: string | null
}

export function usePreviewReport() {
  return useMutation<RenderReport, unknown, PreviewBody>({
    mutationFn: async (body) => (await api.post('/reports/preview', body, { timeout: 180_000 })).data,
  })
}

export function useTemplate(id: string | null | undefined) {
  return useQuery<CustomTemplate>({
    queryKey: ['reports', 'template', id],
    queryFn: async () => (await api.get(`/reports/custom/${id}`)).data,
    enabled: !!id,
  })
}

function useInvalidateReports() {
  const qc = useQueryClient()
  return () => {
    qc.invalidateQueries({ queryKey: ['reports', 'catalog'] })
    qc.invalidateQueries({ queryKey: ['reports', 'template'] })
  }
}

export function useSaveTemplate() {
  const invalidate = useInvalidateReports()
  return useMutation<{ id?: string; message: string }, unknown, { id?: string | null; body: TemplateBody }>({
    mutationFn: async ({ id, body }) =>
      id ? (await api.put(`/reports/custom/${id}`, body)).data : (await api.post('/reports/custom', body)).data,
    onSuccess: invalidate,
  })
}

export function useDuplicateTemplate() {
  const invalidate = useInvalidateReports()
  return useMutation<{ id: string; message: string }, unknown, string>({
    mutationFn: async (id) => (await api.post(`/reports/custom/${id}/duplicate`)).data,
    onSuccess: invalidate,
  })
}

export function useDeleteTemplate() {
  const invalidate = useInvalidateReports()
  return useMutation<{ message: string }, unknown, { id: string; force?: boolean }>({
    mutationFn: async ({ id, force }) => (await api.delete(`/reports/custom/${id}${force ? '?force=true' : ''}`)).data,
    onSuccess: invalidate,
  })
}

export function useReportRuns(limit = 20) {
  return useQuery<ReportRun[]>({
    queryKey: ['reports', 'runs', limit],
    queryFn: async () => (await api.get(`/reports/runs?limit=${limit}`)).data,
  })
}
