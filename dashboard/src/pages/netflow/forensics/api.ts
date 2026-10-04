/**
 * Saved-view and export-job requests shared by Forensics, Saved Views and the overview's "Save
 * view" button. Every write invalidates the saved-view list so a new or changed view shows at once
 * (NF-34), and failures surface as strings (never a 422 array rendered as a React child).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { nf, nfKeys, nfRetry } from '@/lib/netflow/api'
import { apiErrorMessage } from '@/lib/netflow/status'
import type { SavedQueryV2 } from '@/lib/netflow/filters'
import type { ExportJob, ExportJobList, SavedView } from './model'

export interface SavedViewCreate {
  name: string
  description?: string | null
  visibility: 'private' | 'shared'
  pinned: boolean
  kind: SavedQueryV2['kind']
  query: SavedQueryV2 & { path?: string }
}

export interface SavedViewPatch {
  name?: string
  description?: string | null
  visibility?: 'private' | 'shared'
  pinned?: boolean
  query?: SavedQueryV2 & { path?: string }
}

export function useSavedViews() {
  return useQuery({
    queryKey: nfKeys.savedViews(),
    queryFn: ({ signal }) => nf.get<SavedView[]>('/netflow/saved-views', null, { signal }),
    retry: nfRetry,
    staleTime: 0,
  })
}

export function useSavedView(id: string | null) {
  return useQuery({
    queryKey: [...nfKeys.savedViews(), id],
    queryFn: ({ signal }) => nf.get<SavedView>(`/netflow/saved-views/${encodeURIComponent(id as string)}`, null, { signal }),
    enabled: !!id,
    retry: false,
    staleTime: 30_000,
  })
}

export function useSavedViewMutations() {
  const qc = useQueryClient()
  const refresh = () => qc.invalidateQueries({ queryKey: nfKeys.savedViews() })
  const create = useMutation({
    mutationFn: async (body: SavedViewCreate) => (await api.post<SavedView>('/netflow/saved-views', body)).data,
    onSuccess: refresh,
  })
  const update = useMutation({
    mutationFn: async ({ id, patch }: { id: string; patch: SavedViewPatch }) =>
      (await api.patch<SavedView>(`/netflow/saved-views/${encodeURIComponent(id)}`, patch)).data,
    onSuccess: refresh,
  })
  const duplicate = useMutation({
    mutationFn: async (id: string) => (await api.post<SavedView>(`/netflow/saved-views/${encodeURIComponent(id)}/duplicate`, {})).data,
    onSuccess: refresh,
  })
  const remove = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/netflow/saved-views/${encodeURIComponent(id)}`)).data,
    onSuccess: refresh,
  })
  return { create, update, duplicate, remove }
}

export function errorText(err: unknown): string {
  return apiErrorMessage(err, 'The request failed')
}

/* ─── Export jobs ──────────────────────────────────────────────────── */

export const exportJobsKey = ['netflow', 'forensics', 'export-jobs'] as const

export function useExportJobs(enabled: boolean) {
  return useQuery({
    queryKey: exportJobsKey,
    queryFn: async ({ signal }) => (await api.get<ExportJobList>('/netflow/forensics/export-jobs', { signal })).data,
    enabled,
    retry: nfRetry,
    // Poll while a job is queued or running; stop once they are all settled.
    refetchInterval: (q) => (q.state.data?.items.some((j) => j.status === 'queued' || j.status === 'running') ? 2_500 : false),
  })
}

export function useExportJobMutations() {
  const qc = useQueryClient()
  const refresh = () => qc.invalidateQueries({ queryKey: exportJobsKey })
  const create = useMutation({
    mutationFn: async (params: URLSearchParams) =>
      (await api.post<ExportJob>(`/netflow/forensics/export-jobs?${params.toString()}`, null, { timeout: 60_000 })).data,
    onSuccess: refresh,
  })
  const remove = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/netflow/forensics/export-jobs/${encodeURIComponent(id)}`)).data,
    onSuccess: refresh,
  })
  return { create, remove }
}

/** Save a blob the API returned as a file. */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function filenameFrom(disposition: string | undefined, fallback: string): string {
  const m = /filename="?([^";]+)"?/i.exec(disposition ?? '')
  return m ? m[1] : fallback
}

/** Download a finished export job's file (gzipped CSV). */
export async function downloadExportJob(job: ExportJob): Promise<void> {
  const res = await api.get(`/netflow/forensics/export-jobs/${encodeURIComponent(job.id)}/download`, {
    responseType: 'blob',
    timeout: 300_000,
  })
  saveBlob(res.data as Blob, filenameFrom(res.headers['content-disposition'] as string | undefined, job.filename ?? `netflow-export-${job.id}.csv.gz`))
}

/** Stream the complete result as CSV (`netflow.export`); resolves with the exported row count. */
export async function downloadStreamedExport(params: URLSearchParams): Promise<number | null> {
  const res = await api.get(`/netflow/forensics/export?${params.toString()}`, { responseType: 'blob', timeout: 300_000 })
  saveBlob(res.data as Blob, filenameFrom(res.headers['content-disposition'] as string | undefined, 'netflow-forensics.csv'))
  const rows = Number(res.headers['x-exported-rows'])
  return Number.isFinite(rows) ? rows : null
}

/**
 * A failed blob request carries its JSON error as a Blob; turn it back into a readable message.
 */
export async function blobErrorText(err: unknown): Promise<string> {
  const data = (err as { response?: { data?: unknown } } | null)?.response?.data
  if (data instanceof Blob) {
    try {
      const body = JSON.parse(await data.text()) as { detail?: unknown }
      if (typeof body.detail === 'string') return body.detail.replace(/\s*\(ref nf-[0-9a-f]{8}\)\s*$/i, '')
    } catch {
      /* not JSON */
    }
  }
  return errorText(err)
}
