import { api } from '@/lib/api'
import type { ApmRangeKey } from '@/components/apm/ApmRange'

export async function openExemplarTrace(
  navigate: (to: string) => void,
  service: string,
  range: ApmRangeKey | string,
  at: string | number,
  metric: 'p95' | 'p50' | 'error' | 'rps' = 'p95',
  targetMs?: number,
) {
  const atMs = typeof at === 'number' ? at : new Date(at).getTime()
  const qp = new URLSearchParams({ at_ms: String(atMs), metric, range: String(range) })
  if (targetMs != null) qp.set('target_ms', String(targetMs))
  try {
    const { data } = await api.get<{ found: boolean; trace_id?: string }>(
      `/apm/services/${encodeURIComponent(service)}/exemplar?${qp}`,
    )
    if (data.found && data.trace_id) {
      navigate(`/apm/traces/${data.trace_id}`)
      return
    }
  } catch {
    /* fall through to the explorer */
  }
  navigate(`/apm/traces?mode=indexed&service=${encodeURIComponent(service)}&range=${range}`)
}
