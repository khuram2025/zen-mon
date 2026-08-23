import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bug, Check, EyeOff, Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { ErrorStatusBadge } from '@/components/apm/errorShared'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { ApmRangePicker, rangePhrase, useApmRange } from '@/components/apm/ApmRange'
import type { ErrorListResponse } from '@/types/apm'

/**
 * Triage states, in inbox order. `unresolved` is the default view: an inbox
 * that keeps showing issues you already closed never visibly shrinks, so
 * triaging it feels like it does nothing.
 */
const STATUSES = ['unresolved', 'resolved', 'resolved_in_version', 'ignored'] as const
const STATUS_LABEL: Record<string, string> = {
  unresolved: 'Unresolved', resolved: 'Resolved',
  resolved_in_version: 'Resolved in version', ignored: 'Ignored',
}

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
  const qc = useQueryClient()
  const [range, setRange] = useApmRange('24h')
  // Absent param means the default inbox (unresolved); `all` is explicit.
  const status = params.get('status') ?? 'unresolved'
  const service = params.get('service') || ''

  const set = (k: string, v: string) => {
    const n = new URLSearchParams(params)
    if (v) n.set(k, v); else n.delete(k)
    setParams(n, { replace: true })
  }

  const q = useQuery<ErrorListResponse>({
    queryKey: ['apm', 'errors', { range, status, service }],
    queryFn: async () => {
      const qp = new URLSearchParams({ range_: range })
      if (status && status !== 'all') qp.set('status', status)
      if (service) qp.set('service', service)
      return (await api.get(`/apm/errors?${qp}`)).data
    },
    refetchInterval: 15_000,
  })

  const triage = useMutation({
    mutationFn: async ({ groupId, next }: { groupId: string; next: string }) =>
      (await api.patch(`/apm/errors/${groupId}`, { status: next })).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['apm', 'errors'] }),
  })

  const issues = q.data?.issues ?? []
  const counts = q.data?.counts ?? {}
  const services = [...new Set(issues.map((i) => i.service))].sort()

  const chip = (key: string, label: string, count?: number) => (
    <button
      key={key}
      onClick={() => set('status', key === 'unresolved' ? '' : key)}
      className={`rounded-full border px-3 py-1.5 text-xs transition-colors ${
        status === key ? 'border-primary bg-primary text-black' : 'border-border text-muted hover:text-text'
      }`}
    >
      {label}{count !== undefined ? ` (${count})` : ''}
    </button>
  )

  return (
    <div className="space-y-4">
      <ApmPageHeader
        title="Errors"
        description="Exceptions grouped into issues by a stable fingerprint, so one broken code path is one row no matter how many times it fires."
        article="errors"
        actions={<ApmRangePicker value={range} onChange={setRange} />}
      />

      {q.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load errors — {apiErrorMessage(q.error)}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {STATUSES.map((s) => chip(s, STATUS_LABEL[s], counts[s]))}
        {chip('all', 'All', counts.all)}
        <div className="flex-1" />
        <select
          value={service} onChange={(e) => set('service', e.target.value)}
          className="h-9 rounded-md border border-border bg-surface2 px-2 text-sm text-text"
        >
          <option value="">All services</option>
          {services.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        {q.isFetching && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
      </div>

      <Card>
        <CardContent className="p-0">
          {q.isLoading ? (
            <div className="flex items-center justify-center gap-2 py-12 text-muted">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading…
            </div>
          ) : issues.length === 0 ? (
            <div className="flex flex-col items-center gap-2 py-12 text-center text-muted">
              <Bug className="h-6 w-6" />
              <span className="text-sm">
                {status === 'unresolved'
                  ? `No unresolved errors in ${rangePhrase(range)}.`
                  : `No ${STATUS_LABEL[status]?.toLowerCase() ?? status} errors in ${rangePhrase(range)}.`}
              </span>
              {status === 'unresolved' && (counts.all ?? 0) > 0 && (
                <button onClick={() => set('status', 'all')} className="text-xs font-medium text-primary hover:underline">
                  Show all {counts.all} issues
                </button>
              )}
            </div>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th>Error</Th><Th>Service</Th>
                  <Th className="text-right">Events</Th><Th className="text-right">Traces</Th>
                  <Th>Status</Th><Th className="text-right">Last seen</Th><Th />
                </Tr>
              </THead>
              <TBody>
                {issues.map((e) => (
                  <Tr key={e.group_id} className="cursor-pointer hover:bg-surface2"
                    onClick={() => navigate(`/apm/errors/${e.group_id}`)}>
                    <Td>
                      <div className="font-medium text-text">{e.exception_type}</div>
                      <div className="max-w-md truncate text-xs text-muted">{e.message}</div>
                    </Td>
                    <Td className="text-sm">{e.service}</Td>
                    <Td className="text-right font-mono text-xs">{e.occurrences.toLocaleString()}</Td>
                    <Td className="text-right font-mono text-xs">{e.traces.toLocaleString()}</Td>
                    <Td><ErrorStatusBadge status={e.status} /></Td>
                    <Td className="text-right text-xs text-muted">{rel(e.last_seen)}</Td>
                    <Td onClick={(ev) => ev.stopPropagation()}>
                      <div className="flex justify-end gap-1">
                        {e.status !== 'resolved' && (
                          <button
                            title="Mark resolved"
                            disabled={triage.isPending}
                            onClick={() => triage.mutate({ groupId: e.group_id, next: 'resolved' })}
                            className="rounded p-1 text-muted hover:bg-surface2 hover:text-success disabled:opacity-40"
                          >
                            <Check className="h-4 w-4" />
                          </button>
                        )}
                        {e.status !== 'ignored' && (
                          <button
                            title="Ignore"
                            disabled={triage.isPending}
                            onClick={() => triage.mutate({ groupId: e.group_id, next: 'ignored' })}
                            className="rounded p-1 text-muted hover:bg-surface2 hover:text-text disabled:opacity-40"
                          >
                            <EyeOff className="h-4 w-4" />
                          </button>
                        )}
                      </div>
                    </Td>
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
