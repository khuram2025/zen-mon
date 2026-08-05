import { useSearchParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, Bug } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { ErrorStatusBadge } from '@/components/apm/errorShared'

interface ErrorIssue {
  group_id: string; exception_type: string; message: string; service: string
  services: string[]; occurrences: number; traces: number
  first_seen: string; last_seen: string; versions: string[]
  status: string; assignee: string | null; resolved_in_version: string | null
}
interface ErrorList { issues: ErrorIssue[]; counts: Record<string, number> }

const RANGES = ['1h', '6h', '24h', '7d']
const STATUSES = ['unresolved', 'resolved', 'resolved_in_version', 'ignored']

function rel(iso: string) {
  const d = Date.now() - new Date(iso).getTime()
  if (d < 60_000) return `${Math.round(d / 1000)}s ago`
  if (d < 3_600_000) return `${Math.round(d / 60_000)}m ago`
  if (d < 86_400_000) return `${Math.round(d / 3_600_000)}h ago`
  return `${Math.round(d / 86_400_000)}d ago`
}

export function ErrorsInboxPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const range = params.get('range') || '24h'
  const status = params.get('status') || ''
  const service = params.get('service') || ''
  const set = (k: string, v: string) => { const n = new URLSearchParams(params); if (v) n.set(k, v); else n.delete(k); setParams(n, { replace: true }) }

  const q = useQuery<ErrorList>({
    queryKey: ['apm', 'errors', { range, status, service }],
    queryFn: async () => {
      const qp = new URLSearchParams({ range_: range })
      if (status) qp.set('status', status)
      if (service) qp.set('service', service)
      return (await api.get(`/apm/errors?${qp}`)).data
    },
    refetchInterval: 15000,
  })
  const issues = q.data?.issues ?? []
  const counts = q.data?.counts ?? {}

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold text-text">Errors</h1>
        <p className="text-sm text-muted mt-1">Grouped exceptions across all services, with triage.</p>
      </div>

      {q.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load errors — {apiErrorMessage(q.error)}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <button onClick={() => set('status', '')} className={`px-3 py-1.5 rounded-full text-xs border ${!status ? 'bg-primary text-white border-primary' : 'border-border text-muted'}`}>
          All
        </button>
        {STATUSES.map((s) => (
          <button key={s} onClick={() => set('status', s)} className={`px-3 py-1.5 rounded-full text-xs border capitalize ${status === s ? 'bg-primary text-white border-primary' : 'border-border text-muted'}`}>
            {s.replace(/_/g, ' ')} {counts[s] ? `(${counts[s]})` : ''}
          </button>
        ))}
        <div className="flex-1" />
        <select value={range} onChange={(e) => set('range', e.target.value)} className="h-9 rounded-md bg-surface2 border border-border text-sm px-2 text-text">
          {RANGES.map((r) => <option key={r} value={r}>Last {r}</option>)}
        </select>
        <Input className="w-44" placeholder="service" value={service} onChange={(e) => set('service', e.target.value)} />
        {q.isFetching && <Loader2 className="w-4 h-4 animate-spin text-muted" />}
      </div>

      <Card>
        <CardContent className="p-0">
          {q.isLoading ? (
            <div className="flex items-center justify-center gap-2 text-muted py-12"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>
          ) : issues.length === 0 ? (
            <div className="flex flex-col items-center gap-2 text-muted py-12"><Bug className="w-6 h-6" /> No errors in range.</div>
          ) : (
            <Table>
              <THead><Tr><Th>Error</Th><Th>Service</Th><Th className="text-right">Events</Th><Th className="text-right">Traces</Th><Th>Status</Th><Th className="text-right">Last seen</Th></Tr></THead>
              <TBody>
                {issues.map((e) => (
                  <Tr key={e.group_id} className="cursor-pointer hover:bg-surface2" onClick={() => navigate(`/apm/errors/${e.group_id}`)}>
                    <Td>
                      <div className="font-medium text-text">{e.exception_type}</div>
                      <div className="text-xs text-muted truncate max-w-md">{e.message}</div>
                    </Td>
                    <Td className="text-sm">{e.service}</Td>
                    <Td className="text-right font-mono text-xs">{e.occurrences}</Td>
                    <Td className="text-right font-mono text-xs">{e.traces}</Td>
                    <Td><ErrorStatusBadge status={e.status} /></Td>
                    <Td className="text-right text-xs text-muted">{rel(e.last_seen)}</Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
