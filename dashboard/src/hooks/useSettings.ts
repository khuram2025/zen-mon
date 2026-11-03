import { useQuery } from '@tanstack/react-query'
import { regionalTimezone } from '@/lib/regionalTime'

interface TimezoneResponse {
  timezone: string
}

async function fetchTimezone(): Promise<TimezoneResponse> {
  const res = await fetch('/api/v1/settings/timezone')
  if (!res.ok) return { timezone: regionalTimezone() }
  const data: TimezoneResponse = await res.json()
  return data
}

export function useTimezone() {
  const { data } = useQuery({
    queryKey: ['settings', 'regional'],
    queryFn: fetchTimezone,
    staleTime: 5 * 60 * 1000,
    gcTime: 30 * 60 * 1000,
  })
  return data?.timezone || regionalTimezone()
}
