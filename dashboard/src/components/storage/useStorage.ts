import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { storageError } from './storageMath'
import { toast } from '@/components/ui/Toast'
import type { Overview, StorageEvent, StorageStatus } from './types'

export const STORAGE_KEY = ['system', 'storage'] as const

export function useOverview() {
  return useQuery<Overview>({
    queryKey: [...STORAGE_KEY, 'overview'],
    queryFn: async () => (await api.get('/system/storage/overview')).data,
    refetchInterval: 30_000,
    staleTime: 25_000,
  })
}

export function useStorageStatus(enabled = true) {
  return useQuery<StorageStatus>({
    queryKey: [...STORAGE_KEY, 'status'],
    enabled,
    staleTime: 4_000,
    queryFn: async () => (await api.get('/system/storage')).data,
    // Poll quickly while data is being moved off a disk.
    refetchInterval: (q) => ((q.state.data as StorageStatus | undefined)?.layout?.pvmove ? 5_000 : 30_000),
  })
}

export function useStorageEvents(limit = 50) {
  return useQuery<StorageEvent[]>({
    queryKey: [...STORAGE_KEY, 'events', limit],
    queryFn: async () => (await api.get(`/system/storage/events?limit=${limit}`)).data,
    refetchInterval: 30_000,
    staleTime: 25_000,
  })
}

const STORAGE_ACTION_TIMEOUT_MS = 35 * 60_000

/**
 * POST a storage action, toast the outcome and refresh every storage query.
 * `describe` words the success toast from the response; `onFail` returns true
 * when it has reported the error itself.
 */
export function useStorageAction<TBody = void>(
  path: string,
  { title, onDone, describe, onFail }: {
    title: string
    onDone?: (data: any) => void
    describe?: (data: any) => string | null | undefined
    onFail?: (e: any) => boolean
  },
) {
  const qc = useQueryClient()
  return useMutation({
    // Disk work outlasts the 30 s default: the server waits up to 5 minutes for adding or claiming a disk and
    // 30 for growing a volume. A browser timeout showed "failed" while the resize was still running.
    mutationFn: async (body: TBody) => (await api.post(path, body ?? undefined, { timeout: STORAGE_ACTION_TIMEOUT_MS })).data,
    onSuccess: (data: any) => {
      toast.success(title, describe?.(data) || 'Storage information refreshed.')
      qc.invalidateQueries({ queryKey: STORAGE_KEY })
      onDone?.(data)
    },
    onError: (e: any) => {
      if (!onFail?.(e)) toast.error(`${title} failed`, storageError(e))
      qc.invalidateQueries({ queryKey: STORAGE_KEY })
    },
  })
}
