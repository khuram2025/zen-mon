import { useQuery } from '@tanstack/react-query'

interface TimezoneResponse {
  timezone: string
}

async function fetchTimezone(): Promise<string> {
  const res = await fetch('/api/v1/settings/timezone')
  if (!res.ok) return 'UTC'
  const data: TimezoneResponse = await res.json()
  return data.timezone || 'UTC'
}

export function useTimezone() {
  const { data: timezone = 'UTC' } = useQuery({
    queryKey: ['settings', 'timezone'],
    queryFn: fetchTimezone,
    staleTime: 5 * 60 * 1000,
    gcTime: 30 * 60 * 1000,
  })
  return timezone
}
