import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { ServiceMetricResponse } from '@/types'
import { DailyProbeFailureDetails } from './DailyProbeFailureDetails'
import { buildProbeFailures, loadProbeHistory } from './probeFailures'

export function DailyProbeFailures({ checkId, start, end, intervalSeconds, downtimeSeconds, hasCoverage }: {
  checkId: string
  start: number
  end: number
  intervalSeconds: number
  downtimeSeconds: number
  hasCoverage: boolean
}) {
  const query = useQuery({
    queryKey: ['service-day-probe-failures', checkId, start, end, intervalSeconds],
    queryFn: async ({ signal }) => {
      const windowEnd = Math.min(end, Date.now())
      const points = await loadProbeHistory(async (from, to) => {
        const response = await api.get<ServiceMetricResponse>(`/service-checks/${checkId}/metrics`, {
          params: { from: new Date(from).toISOString(), to: new Date(to).toISOString(), granularity: 'raw' }, signal,
        })
        return response.data.points
      }, start - Math.max(1, intervalSeconds) * 2000, windowEnd)
      return buildProbeFailures(points, start, windowEnd, intervalSeconds)
    },
    refetchInterval: end > Date.now() ? 30_000 : false,
    staleTime: 30_000,
  })
  return <DailyProbeFailureDetails failures={query.data} loading={query.isLoading} error={query.isError}
    onRetry={() => { void query.refetch() }} downtimeSeconds={downtimeSeconds} hasCoverage={hasCoverage} />
}
