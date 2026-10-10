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

/** POST a storage action, toast the outcome and refresh every storage query. */
export function useStorageAction<TBody = void>(
  path: string,
  { title, onDone }: { title: string; onDone?: (data: any) => void },
) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: TBody) => (await api.post(path, body ?? undefined)).data,
    onSuccess: (data: any) => {
      toast.success(title, 'Storage information refreshed.')
      qc.invalidateQueries({ queryKey: STORAGE_KEY })
      onDone?.(data)
    },
    onError: (e: any) => {
      toast.error(`${title} failed`, storageError(e))
      qc.invalidateQueries({ queryKey: STORAGE_KEY })
    },
  })
}
