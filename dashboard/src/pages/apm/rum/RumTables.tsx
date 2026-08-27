import type { KeyboardEvent } from 'react'
import {
  CheckCircle2,
  FileWarning,
  Layers3,
  Loader2,
  MousePointerClick,
  Network,
  Users,
} from 'lucide-react'
import { formatBytes, relativeTime } from '@/lib/utils'
import { fmtPct } from '@/components/apm/shared'
import { fmtCount } from '@/components/apm/viz'
import { DurationTimeline, EXPLORER_HEAD } from '@/components/apm/explorer'
import { Badge } from '@/components/ui/Badge'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import type {
  RumAction,
  RumError,
  RumListResponse,
  RumResource,
  RumSession,
  RumView,
} from '@/types/apm'
import type { RumSortOrder } from './useRumUrlState'
import {
  QueryErrorPanel,
  RumCoverageNotice,
  RumEmptyState,
  RumMetricCell,
  RumPager,
  RumSamplingNotice,
  RumTableCard,
  SortableTh,
  TracePivot,
} from './RumUi'

interface SharedProps {
  page: number
  pageSize: number
  sort: string
  order: RumSortOrder
  loading: boolean
  error: unknown
  filtered: boolean
  onSort: (key: string) => void
  onPage: (page: number) => void
  onPageSize: (size: number) => void
  onRetry: () => void
  embedded?: boolean
}

const INTERACTIVE_ROW = 'cursor-pointer focus:outline-none focus-visible:bg-surface2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/40'

function onRowKey(event: KeyboardEvent<HTMLTableRowElement>, open: () => void) {
  if (event.key !== 'Enter' && event.key !== ' ') return
  event.preventDefault()
  open()
}

function LoadingRow({ columns }: { columns: number }) {
  return <Tr className="hover:bg-transparent"><Td colSpan={columns} className="py-12 text-center"><span className="inline-flex items-center gap-2 text-xs text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading real-user data…</span></Td></Tr>
}

function EmptyRow({ columns, filtered, noun, icon }: { columns: number; filtered: boolean; noun: string; icon: typeof Layers3 }) {
  return <Tr className="hover:bg-transparent"><Td colSpan={columns}><RumEmptyState icon={icon} title={`No ${noun} found`} description={filtered ? 'No records match the current range and segment filters. Clear or broaden the filters to continue.' : `No ${noun} have been received in this time range.`} /></Td></Tr>
}

function pageFooter(data: { total: number } | undefined, props: SharedProps, noun: string) {
  return <RumPager page={props.page} pageSize={props.pageSize} total={data?.total ?? 0} noun={noun} onPage={props.onPage} onPageSize={props.onPageSize} />
}

export function viewRowKey(view: RumView): string {
  return [view.application_id, view.env, view.view_name].join('\u001f')
}

export function errorRowKey(error: RumError): string {
  return error.fingerprint
}

export function resourceRowKey(resource: RumResource): string {
  return [resource.application_id, resource.env, resource.view_name, resource.name, resource.method ?? '', resource.status_code ?? ''].join('\u001f')
}

export function actionRowKey(action: RumAction): string {
  return [action.application_id, action.env, action.view_name, action.action_type, action.name, action.target ?? ''].join('\u001f')
}

export function RumViewsTable({ data, onOpen, ...props }: SharedProps & { data?: RumListResponse<RumView>; onOpen: (row: RumView) => void }) {
  if (props.error) return <QueryErrorPanel label="view analytics" error={props.error} onRetry={props.onRetry} />
  return (
    <RumTableCard embedded={props.embedded} title="Views" description="Route-level traffic, Core Web Vitals and error impact. Open a row for experience detail and related explorers." notice={<RumCoverageNotice coverage={data?.coverage} />} footer={pageFooter(data, props, 'views')}>
      <Table>
        <THead className={EXPLORER_HEAD}><Tr>
          <Th>Application / view</Th>
          <SortableTh label="Views" sortKey="views" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Sessions" sortKey="sessions" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="LCP p75" sortKey="lcp_p75" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="INP p75" sortKey="inp_p75" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="CLS p75" sortKey="cls_p75" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Error sessions" sortKey="error_session_rate" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Last seen" sortKey="last_seen" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
        </Tr></THead>
        <TBody>
          {props.loading ? <LoadingRow columns={8} /> : !data?.items.length ? <EmptyRow columns={8} filtered={props.filtered} noun="views" icon={Layers3} /> : data.items.map((view) => (
            <Tr key={viewRowKey(view)} className={INTERACTIVE_ROW} tabIndex={0} aria-label={`Open view ${view.view_name || '/'}`} onClick={() => onOpen(view)} onKeyDown={(event) => onRowKey(event, () => onOpen(view))}>
              <Td>
                <button type="button" className="max-w-[300px] truncate font-mono text-xs font-medium text-text hover:underline" title={view.view_name} onClick={(event) => { event.stopPropagation(); onOpen(view) }}>
                  {view.view_name || '/'}
                </button>
                <div className="text-[10px] text-muted">{view.application_id} · {view.env}</div>
              </Td>
              <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(view.views)}</Td>
              <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(view.sessions)}</Td>
              <Td><RumMetricCell name="lcp" value={view.lcp_p75} samples={view.lcp_samples} /></Td>
              <Td><RumMetricCell name="inp" value={view.inp_p75} samples={view.inp_samples} /></Td>
              <Td><RumMetricCell name="cls" value={view.cls_p75} samples={view.cls_samples} /></Td>
              <Td className="text-right"><span className={(view.error_session_rate ?? 0) >= 0.05 ? 'font-mono text-xs text-danger' : 'font-mono text-xs'}>{fmtPct(view.error_session_rate)}</span><div className="text-[9px] text-muted">{fmtCount(view.errors)} errors</div></Td>
              <Td className="whitespace-nowrap text-right text-xs text-muted">{view.last_seen ? relativeTime(view.last_seen) : '—'}</Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </RumTableCard>
  )
}

export function RumSessionsTable({ data, onOpen, ...props }: SharedProps & { data?: RumListResponse<RumSession>; onOpen: (row: RumSession) => void }) {
  if (props.error) return <QueryErrorPanel label="sessions" error={props.error} onRetry={props.onRetry} />
  const maxDuration = Math.max(...(data?.items.map((session) => session.duration_ms) ?? [1]), 1)
  return (
    <RumTableCard embedded={props.embedded} title="Sessions" description="Real-user journeys with frontend events and backend trace correlation. Open a row for the session timeline." notice={<RumCoverageNotice coverage={data?.coverage} />} footer={pageFooter(data, props, 'sessions')}>
      <Table>
        <THead className={EXPLORER_HEAD}><Tr>
          <Th>Session</Th><Th>Client</Th>
          <SortableTh label="Duration" sortKey="duration_ms" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Views" sortKey="views" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Actions" sortKey="actions" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Errors" sortKey="errors" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Last seen" sortKey="last_seen" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <Th>Backend trace</Th>
        </Tr></THead>
        <TBody>
          {props.loading ? <LoadingRow columns={8} /> : !data?.items.length ? <EmptyRow columns={8} filtered={props.filtered} noun="sessions" icon={Users} /> : data.items.map((session) => (
            <Tr key={session.session_id} className={INTERACTIVE_ROW} tabIndex={0} aria-label={`Open session ${session.session_id.slice(0, 14)}`} onClick={() => onOpen(session)} onKeyDown={(event) => onRowKey(event, () => onOpen(session))}>
              <Td>
                <button type="button" className="font-mono text-xs font-medium text-text hover:underline" onClick={(event) => { event.stopPropagation(); onOpen(session) }}>
                  {session.session_id.slice(0, 14)}…
                </button>
                <div className="text-[10px] text-muted">{session.user_id || session.application_id} · {session.env}{session.service_version ? ` · ${session.service_version}` : ''}</div>
              </Td>
              <Td><div className="text-xs text-text2">{session.browser || 'Unknown'}{session.browser_version ? ` ${session.browser_version}` : ''} · {session.device_type || 'unknown'}</div><div className="text-[10px] text-muted">{[session.os, session.country].filter(Boolean).join(' · ') || 'Unknown location'}</div></Td>
              <Td className="text-right"><DurationTimeline ms={session.duration_ms} maxMs={maxDuration} significant={session.errors > 0 || session.duration_ms >= 30_000} /></Td>
              <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(session.views)}</Td>
              <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(session.actions)}</Td>
              <Td className="text-right">{session.errors ? <Badge variant="danger"><FileWarning className="h-3 w-3" />{session.errors}</Badge> : <Badge variant="success"><CheckCircle2 className="h-3 w-3" />0</Badge>}</Td>
              <Td className="whitespace-nowrap text-right text-xs text-muted">{relativeTime(session.last_seen)}</Td>
              <Td><TracePivot traceId={session.backend_trace_id || session.backend_trace_ids?.[0]} compact /></Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </RumTableCard>
  )
}

export function RumErrorsTable({ data, onOpen, ...props }: SharedProps & { data?: RumListResponse<RumError>; onOpen: (row: RumError) => void }) {
  if (props.error) return <QueryErrorPanel label="browser errors" error={props.error} onRetry={props.onRetry} />
  return (
    <RumTableCard embedded={props.embedded} title="JavaScript errors" description="Grouped browser failures ranked by affected sessions. Open a row for stack, client context and the correlated trace." notice={<><RumCoverageNotice coverage={data?.coverage} /><RumSamplingNotice sampling={data?.sampling} /></>} footer={pageFooter(data, props, 'issues')}>
      <Table>
        <THead className={EXPLORER_HEAD}><Tr>
          <Th>Error</Th><Th>Application / view</Th>
          <SortableTh label="Events" sortKey="count" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Sessions" sortKey="sessions" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="First seen" sortKey="first_seen" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Last seen" sortKey="last_seen" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <Th>Trace</Th>
        </Tr></THead>
        <TBody>
          {props.loading ? <LoadingRow columns={7} /> : !data?.items.length ? <EmptyRow columns={7} filtered={props.filtered} noun="errors" icon={FileWarning} /> : data.items.map((error) => (
            <Tr key={errorRowKey(error)} className={INTERACTIVE_ROW} tabIndex={0} aria-label={`Open error ${error.error_type || error.message}`} onClick={() => onOpen(error)} onKeyDown={(event) => onRowKey(event, () => onOpen(error))}>
              <Td>
                <button type="button" className="max-w-[360px] truncate text-xs font-medium text-text hover:underline" title={error.message} onClick={(event) => { event.stopPropagation(); onOpen(error) }}>
                  {error.message}
                </button>
                <div className="max-w-[360px] truncate font-mono text-[10px] text-muted">{error.error_type || error.source || error.fingerprint}</div>
              </Td>
              <Td><div className="text-xs text-text2">{error.application_id} · {error.env}{error.service_version ? ` · ${error.service_version}` : ''}</div><div className="max-w-[220px] truncate font-mono text-[10px] text-muted">{error.view_name || '/'}</div></Td>
              <Td className="text-right"><div className="font-mono text-xs font-semibold tabular-nums text-danger">{fmtCount(error.count)}</div>{(error.sampled_count != null || error.unsampled_count != null) && <div className="text-[9px] text-muted">{fmtCount(error.sampled_count)} sampled{(error.unsampled_count ?? 0) > 0 ? ` · ${fmtCount(error.unsampled_count)} retained` : ''}</div>}</Td>
              <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(error.sessions)}</Td>
              <Td className="whitespace-nowrap text-right text-xs text-muted">{relativeTime(error.first_seen)}</Td>
              <Td className="whitespace-nowrap text-right text-xs text-muted">{relativeTime(error.last_seen)}</Td>
              <Td><TracePivot traceId={error.backend_trace_id} compact /></Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </RumTableCard>
  )
}

export function RumResourcesTable({ data, onOpen, ...props }: SharedProps & { data?: RumListResponse<RumResource>; onOpen: (row: RumResource) => void }) {
  if (props.error) return <QueryErrorPanel label="resource performance" error={props.error} onRetry={props.onRetry} />
  const maxDuration = Math.max(...(data?.items.map((resource) => resource.duration_p75 ?? 0) ?? [1]), 1)
  return (
    <RumTableCard embedded={props.embedded} title="Resources" description="Fetch, XHR and static asset performance, including failure rate and payload size." notice={<RumCoverageNotice coverage={data?.coverage} />} footer={pageFooter(data, props, 'resources')}>
      <Table>
        <THead className={EXPLORER_HEAD}><Tr>
          <Th>Resource</Th><Th>View</Th>
          <SortableTh label="Requests" sortKey="count" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Failed" sortKey="failed_count" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Duration p75" sortKey="duration_p75" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Avg size" sortKey="size_avg" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Last seen" sortKey="last_seen" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <Th>Trace</Th>
        </Tr></THead>
        <TBody>
          {props.loading ? <LoadingRow columns={8} /> : !data?.items.length ? <EmptyRow columns={8} filtered={props.filtered} noun="resources" icon={Network} /> : data.items.map((resource) => {
            const failedRate = resource.failure_rate ?? (resource.count ? resource.failed_count / resource.count : null)
            return (
              <Tr key={resourceRowKey(resource)} className={INTERACTIVE_ROW} tabIndex={0} aria-label={`Open resource ${resource.name || resource.url || 'details'}`} onClick={() => onOpen(resource)} onKeyDown={(event) => onRowKey(event, () => onOpen(resource))}>
                <Td>
                  <div className="flex items-center gap-1.5"><Badge variant="outline" className="uppercase">{resource.method || resource.resource_type || 'asset'}</Badge>{resource.status_code != null && <span className={resource.status_code >= 400 ? 'font-mono text-[10px] text-danger' : 'font-mono text-[10px] text-muted'}>{resource.status_code}</span>}</div>
                  <button type="button" className="mt-1 max-w-[320px] truncate font-mono text-[10px] text-text hover:underline" title={resource.url || resource.name} onClick={(event) => { event.stopPropagation(); onOpen(resource) }}>
                    {resource.name || resource.url}
                  </button>
                </Td>
                <Td><div className="max-w-[220px] truncate font-mono text-xs text-text2">{resource.view_name || '/'}</div><div className="text-[10px] text-muted">{resource.application_id} · {resource.env}</div></Td>
                <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(resource.count)}</Td>
                <Td className="text-right"><span className={resource.failed_count ? 'font-mono text-xs text-danger' : 'font-mono text-xs text-success'}>{fmtCount(resource.failed_count)}</span><div className="text-[9px] text-muted">{fmtPct(failedRate)}</div></Td>
                <Td className="text-right">{resource.duration_p75 == null ? <span className="text-xs text-muted">—</span> : <DurationTimeline ms={resource.duration_p75} maxMs={maxDuration} significant={(resource.failed_count > 0) || resource.duration_p75 >= 1000} />}</Td>
                <Td className="text-right"><div className="font-mono text-xs tabular-nums">{resource.size_avg == null ? '—' : formatBytes(resource.size_avg)}</div>{resource.size_samples != null && <div className="text-[9px] text-muted">n={resource.size_samples.toLocaleString()}</div>}</Td>
                <Td className="whitespace-nowrap text-right text-xs text-muted">{relativeTime(resource.last_seen)}</Td>
                <Td><TracePivot traceId={resource.backend_trace_id} compact /></Td>
              </Tr>
            )
          })}
        </TBody>
      </Table>
    </RumTableCard>
  )
}

export function RumActionsTable({ data, onOpen, ...props }: SharedProps & { data?: RumListResponse<RumAction>; onOpen: (row: RumAction) => void }) {
  if (props.error) return <QueryErrorPanel label="user actions" error={props.error} onRetry={props.onRetry} />
  const maxDuration = Math.max(...(data?.items.map((action) => action.duration_p75 ?? 0) ?? [1]), 1)
  return (
    <RumTableCard embedded={props.embedded} title="User actions" description="Clicks, interactions and frustration signals such as rage clicks and dead clicks." notice={<RumCoverageNotice coverage={data?.coverage} />} footer={pageFooter(data, props, 'actions')}>
      <Table>
        <THead className={EXPLORER_HEAD}><Tr>
          <Th>Action</Th><Th>Target / view</Th>
          <SortableTh label="Count" sortKey="count" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Duration p75" sortKey="duration_p75" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Errors" sortKey="error_count" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <SortableTh label="Last seen" sortKey="last_seen" activeSort={props.sort} order={props.order} onSort={props.onSort} className="text-right" />
          <Th>Trace</Th>
        </Tr></THead>
        <TBody>
          {props.loading ? <LoadingRow columns={7} /> : !data?.items.length ? <EmptyRow columns={7} filtered={props.filtered} noun="actions" icon={MousePointerClick} /> : data.items.map((action) => {
            const frustration = ['rage_click', 'dead_click', 'error_click'].includes(action.action_type)
            return (
              <Tr key={actionRowKey(action)} className={INTERACTIVE_ROW} tabIndex={0} aria-label={`Open action ${action.name || 'details'}`} onClick={() => onOpen(action)} onKeyDown={(event) => onRowKey(event, () => onOpen(action))}>
                <Td>
                  <button type="button" className="text-xs font-medium text-text hover:underline" onClick={(event) => { event.stopPropagation(); onOpen(action) }}>
                    {action.name || 'Unnamed action'}
                  </button>
                  <Badge variant={frustration ? 'warning' : 'outline'} className="mt-1">{(action.action_type || 'action').replaceAll('_', ' ')}</Badge>
                </Td>
                <Td><div className="max-w-[280px] truncate font-mono text-[10px] text-text2" title={action.target}>{action.target || 'No target label'}</div><div className="max-w-[240px] truncate text-[10px] text-muted">{action.view_name || '/'} · {action.application_id}</div></Td>
                <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(action.count)}</Td>
                <Td className="text-right">{action.duration_p75 == null ? <span className="text-xs text-muted">—</span> : <DurationTimeline ms={action.duration_p75} maxMs={maxDuration} significant={action.error_count > 0 || action.duration_p75 >= 1000} />}</Td>
                <Td className="text-right"><span className={action.error_count ? 'font-mono text-xs text-danger' : 'font-mono text-xs text-success'}>{fmtCount(action.error_count)}</span></Td>
                <Td className="whitespace-nowrap text-right text-xs text-muted">{relativeTime(action.last_seen)}</Td>
                <Td><TracePivot traceId={action.backend_trace_id} compact /></Td>
              </Tr>
            )
          })}
        </TBody>
      </Table>
    </RumTableCard>
  )
}
