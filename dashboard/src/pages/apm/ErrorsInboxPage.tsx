import { useMemo } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bug, Check, Download, EyeOff, Loader2, RefreshCw, Search } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { ErrorStatusBadge } from '@/components/apm/errorShared'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { ApmRangePicker, rangePhrase, useApmRange } from '@/components/apm/ApmRange'
import { ApmKpi, RankBar, fmtCount } from '@/components/apm/viz'
import {
  ApmExplorerFrame,
  ApmFacetSidebar,
  ApmUnderlineNav,
  EXPLORER_HEAD,
  VolumeHistogram,
  bucketByTime,
  downloadCsv,
} from '@/components/apm/explorer'
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
  const status = params.get('status') ?? 'unresolved'
  const service = params.get('service') || ''
  const search = params.get('q') || ''

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
  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase()
    if (!needle) return issues
    return issues.filter((issue) =>
      (issue.exception_type || '').toLowerCase().includes(needle)
      || (issue.message || '').toLowerCase().includes(needle)
      || (issue.service || '').toLowerCase().includes(needle))
  }, [issues, search])
  const serviceFacets = useMemo(() => {
    const tally = new Map<string, number>()
    for (const issue of issues) tally.set(issue.service, (tally.get(issue.service) ?? 0) + 1)
    return [...tally.entries()].sort((a, b) => b[1] - a[1]).map(([value, count]) => ({ value, count }))
  }, [issues])
  const maxOcc = Math.max(...visible.map((i) => i.occurrences), 1)
  const totalOcc = visible.reduce((a, i) => a + i.occurrences, 0)
  const servicesHit = new Set(issues.map((i) => i.service)).size

  return (
    <div className="space-y-4">
      <ApmPageHeader
        title="Errors"
        description="Exceptions grouped into issues by a stable fingerprint, so one broken code path is one row no matter how many times it fires."
        article="errors"
        actions={<ApmRangePicker value={range} onChange={setRange} />}
      />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <ApmKpi label="Unresolved" icon={<Bug className="h-4 w-4" />} tone={(counts.unresolved ?? 0) ? 'danger' : 'success'} value={fmtCount(counts.unresolved)} sub="open issues" />
        <ApmKpi label="All issues" tone="info" value={fmtCount(counts.all)} sub={rangePhrase(range)} />
        <ApmKpi label="Events shown" tone="warning" value={fmtCount(totalOcc)} sub="in this filter" />
        <ApmKpi label="Services hit" tone="accent" value={fmtCount(servicesHit)} sub="distinct services" />
      </div>

      {q.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load errors — {apiErrorMessage(q.error)}
        </div>
      )}

      <ApmUnderlineNav
        items={[
          ...STATUSES.map((key) => ({
            key,
            label: STATUS_LABEL[key],
            count: counts[key],
            current: status === key,
            onSelect: () => set('status', key === 'unresolved' ? '' : key),
          })),
          { key: 'all', label: 'All', count: counts.all, current: status === 'all', onSelect: () => set('status', 'all') },
        ]}
      />

      <ApmExplorerFrame
        search={
          <div className="relative min-w-[14rem] flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input className="pl-8" placeholder="Search type, message, or service" value={search} onChange={(e) => set('q', e.target.value)} />
          </div>
        }
        actions={
          <>
            <Button variant="outline" size="sm" onClick={() => q.refetch()} disabled={q.isFetching}>
              <RefreshCw className={`h-3.5 w-3.5 ${q.isFetching ? 'animate-spin' : ''}`} /> Refresh
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={!visible.length}
              onClick={() => downloadCsv(
                'apm-errors.csv',
                ['type', 'message', 'service', 'occurrences', 'traces', 'status', 'last_seen'],
                visible.map((issue) => [issue.exception_type, issue.message, issue.service, issue.occurrences, issue.traces, issue.status, issue.last_seen]),
              )}
            >
              <Download className="h-3.5 w-3.5" /> Export
            </Button>
          </>
        }
        summary={<>Displaying {fmtCount(visible.length)} issues · {fmtCount(totalOcc)} events · {rangePhrase(range)}</>}
        histogram={<VolumeHistogram buckets={bucketByTime(visible, (issue) => issue.last_seen, (issue) => issue.status === 'unresolved')} okLabel="Other" errLabel="Unresolved" />}
        sidebar={
          <ApmFacetSidebar
            title="Error analytics"
            groups={[{
              title: 'Service',
              items: serviceFacets.map((item) => ({
                ...item,
                active: service === item.value,
                onSelect: () => set('service', service === item.value ? '' : item.value),
              })),
            }]}
          />
        }
      >
        {q.isLoading ? (
          <div className="flex items-center justify-center gap-2 py-12 text-muted">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading…
          </div>
        ) : visible.length === 0 ? (
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
            <THead className={EXPLORER_HEAD}>
              <Tr>
                <Th>Error</Th><Th>Service</Th>
                <Th className="text-right">Events</Th><Th className="text-right">Traces</Th>
                <Th>Status</Th><Th className="text-right">Last seen</Th><Th />
              </Tr>
            </THead>
            <TBody>
              {visible.map((e) => (
                <Tr key={e.group_id} className="cursor-pointer" tabIndex={0} aria-label={`Open issue ${e.exception_type}`} onClick={() => navigate(`/apm/errors/${e.group_id}`)}>
                  <Td>
                    <div className="font-medium text-text">{e.exception_type}</div>
                    <div className="max-w-md truncate text-xs text-muted">{e.message}</div>
                  </Td>
                  <Td className="text-sm">
                    <button
                      className="text-primary hover:underline"
                      onClick={(ev) => { ev.stopPropagation(); navigate(`/apm/services/${encodeURIComponent(e.service)}`) }}
                    >
                      {e.service}
                    </button>
                  </Td>
                  <Td className="min-w-[5.5rem] text-right">
                    <div className="font-mono text-xs tabular-nums">{e.occurrences.toLocaleString()}</div>
                    <RankBar value={e.occurrences} max={maxOcc} color="#db2777" />
                  </Td>
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
      </ApmExplorerFrame>
    </div>
  )
}
