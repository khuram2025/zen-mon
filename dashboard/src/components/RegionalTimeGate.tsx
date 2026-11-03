import { useLayoutEffect, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { DEFAULT_BRANDING, type Branding } from '@/lib/branding'
import { setRegionalTime } from '@/lib/regionalTime'

/** Mount pages only after the configured zone is ready; refresh on settings saves. */
export function RegionalTimeGate({ children }: { children: ReactNode }) {
  const query = useQuery<Branding>({ queryKey: ['settings', 'regional'],
    queryFn: async () => (await api.get('/settings/timezone')).data, staleTime: 60_000, refetchInterval: 60_000 })
  const value = query.data ?? DEFAULT_BRANDING
  const key = `${value.timezone}|${value.date_format}|${value.time_format}`
  const [applied, setApplied] = useState('')
  useLayoutEffect(() => { setRegionalTime(value); setApplied(key) }, [value, key])
  if (query.isPending || applied !== key) return <div className="p-6 text-sm text-muted">Loading regional settings…</div>
  return <div key={key} className="contents">{children}</div>
}
