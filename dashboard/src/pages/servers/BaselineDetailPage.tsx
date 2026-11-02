import { Fragment, useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowLeft, ArrowUp, ArrowUpDown, CheckCircle2, ChevronDown, ChevronRight, ClipboardCheck, Clock3, ExternalLink, ListChecks, Pencil, RefreshCw, Search, Server, ShieldAlert } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { useStableListRows } from '@/hooks/useStableListRows'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Skeleton } from '@/components/ui/Skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { toast } from '@/components/ui/Toast'
import { KpiTile, TagList } from '@/components/servers/shared'
import { EmptyState, QueryError, TablePager, usePagedRows } from '@/components/servers/tables'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { BaselineFormDialog } from './BaselinesPage'
import type { BaselineCoverageReport, BaselineRule, BaselineRuleCheck } from '@/types/servers'

const SERVER_STATES = {
  all: 'All servers', violated: 'Violated', compliant: 'Compliant', not_evaluated: 'Needs evaluation',
} as const
const SORTS = ['status', 'server_name', 'primary_ip', 'violations', 'checked', 'evaluated_at'] as const
type Sort = typeof SORTS[number]
const LABELS = { compliant: 'Compliant', violated: 'Violated', missing: 'Missing', outdated: 'Outdated', prohibited: 'Prohibited', not_evaluated: 'Not evaluated' }
const variant = (status: string) => status === 'compliant' ? 'success' : status === 'not_evaluated' ? 'outline' : status === 'outdated' ? 'warning' : 'danger'
const dateTitle = (value: string | null) => value ? new Date(value).toLocaleString() : undefined
const when = (value: string | null) => value ? relativeTime(value) : 'Not evaluated'

function expectation(rule: Pick<BaselineRule, 'rule_type' | 'package_match' | 'match_type' | 'min_version'>) {
  return `${rule.rule_type === 'prohibited' ? 'Must not be installed' : 'Must be installed'}: ${rule.package_match}${rule.rule_type === 'required' && rule.min_version ? ` · version ${rule.min_version} or later` : ''}`
}

export function BaselineDetailPage() {
  const { baselineId = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const qc = useQueryClient()
  const canManage = useCan()('servers.manage')
  const [editing, setEditing] = useState(false)
  const q = params.get('q') || ''
  const [search, setSearch] = useState(q)
  const rawStatus = params.get('status') || 'all'
  const status = Object.prototype.hasOwnProperty.call(SERVER_STATES, rawStatus) ? rawStatus as keyof typeof SERVER_STATES : 'all'
  const rawSort = params.get('sort') || 'status'
  const sort: Sort = SORTS.includes(rawSort as Sort) ? rawSort as Sort : 'status'
  const order = params.get('order') === 'desc' ? 'desc' : 'asc'
  const page = Math.max(1, Math.min(1_000_000, Number.parseInt(params.get('page') || '1', 10) || 1))
  const pageSize = [25, 50, 100, 250].includes(Number(params.get('size'))) ? Number(params.get('size')) : 50
  const view = params.get('view') === 'rules' ? 'rules' : 'servers'
  const expanded = params.get('server')
  const update = (values: Record<string, string | null>, resetPage = true) => setParams(previous => {
    const next = new URLSearchParams(previous)
    if (resetPage) { next.delete('page'); next.delete('server') }
    for (const [key, value] of Object.entries(values)) value == null ? next.delete(key) : next.set(key, value)
    return next
  })
  useEffect(() => setSearch(q), [q])
  useEffect(() => {
    if (search === q) return
    const timer = setTimeout(() => setParams(previous => {
      const next = new URLSearchParams(previous)
      search ? next.set('q', search) : next.delete('q')
      next.delete('page'); next.delete('server')
      return next
    }, { replace: true }), 300)
    return () => clearTimeout(timer)
  }, [search, q, setParams])

  const query = useQuery<BaselineCoverageReport>({
    queryKey: ['server-baselines', baselineId, 'coverage', q, status, sort, order, page, pageSize],
    queryFn: async () => (await api.get(`/server-baselines/${baselineId}/coverage`, { params: { q, status, sort, order, page, page_size: pageSize } })).data,
    placeholderData: previous => previous?.baseline.id === baselineId ? previous : undefined,
    refetchInterval: expanded ? false : 60_000,
    refetchOnWindowFocus: !expanded,
    refetchOnReconnect: !expanded,
  })
  const data = query.data
  const baseline = data?.baseline
  const summary = data?.summary
  useEffect(() => {
    if (!data || query.isPlaceholderData) return
    const last = Math.max(1, Math.ceil(data.total / pageSize))
    if (page <= last) return
    setParams(previous => {
      const next = new URLSearchParams(previous)
      next.set('page', String(last)); next.delete('server')
      return next
    }, { replace: true })
  }, [data, page, pageSize, setParams, query.isPlaceholderData])
  useEffect(() => {
    if (!data || query.isPlaceholderData || !expanded || data.items.some(row => row.server_id === expanded)) return
    setParams(previous => { const next = new URLSearchParams(previous); next.delete('server'); return next }, { replace: true })
  }, [data, expanded, setParams, query.isPlaceholderData])
  const items = useStableListRows(data?.items || [], r => r.server_id, !!expanded, JSON.stringify([baselineId, q, status, sort, order, page, pageSize]))
  const evaluate = useMutation({
    mutationFn: async () => (await api.post(`/server-baselines/${baselineId}/evaluate`)).data,
    onSuccess: () => {
      toast.success('Evaluation complete', 'Results checked against the latest received software inventory')
      qc.invalidateQueries({ queryKey: ['server-baselines'] })
      qc.invalidateQueries({ queryKey: ['servers'] })
    },
    onError: error => toast.error('Evaluation failed', apiErrorMessage(error)),
  })
  const changeSort = (key: Sort) => update({ sort: key, order: sort === key && order === 'asc' ? 'desc' : 'asc' })
  const heading = (key: Sort, label: string) => {
    const Icon = sort !== key ? ArrowUpDown : order === 'asc' ? ArrowUp : ArrowDown
    return <Th aria-sort={sort === key ? order === 'asc' ? 'ascending' : 'descending' : 'none'}><button type="button" className="inline-flex items-center gap-1 hover:text-primary" aria-label={`Sort by ${label}`} onClick={() => changeSort(key)}>{label}<Icon className={cn('h-3 w-3', sort === key ? 'text-primary' : 'text-muted')} /></button></Th>
  }
  const back = <Button asChild variant="ghost" size="sm"><Link to="/server-baselines"><ArrowLeft className="h-4 w-4" /> All baselines</Link></Button>
  if (query.isError) return <div className="space-y-4">{back}<Card><CardContent><QueryError error={query.error} onRetry={() => query.refetch()} /></CardContent></Card></div>
  if (!data || !baseline || !summary) return <div className="space-y-4">{back}<Skeleton className="h-24 w-full" /><div className="grid grid-cols-2 gap-3 lg:grid-cols-4">{[0, 1, 2, 3].map(i => <Skeleton key={i} className="h-24" />)}</div><Skeleton className="h-80 w-full" /></div>

  return <div className="space-y-4">
    {back}
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 space-y-2">
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight"><ClipboardCheck className="h-5 w-5 shrink-0 text-primary" />{baseline.name}</h1>
        {baseline.description && <p className="text-sm text-text2">{baseline.description}</p>}
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted"><Badge variant={baseline.enabled ? 'success' : 'outline'}>{baseline.enabled ? 'Enabled' : 'Disabled'}</Badge><Badge variant="outline">{baseline.os_type || 'Any OS'}</Badge>{baseline.site_name && <Badge variant="outline">{baseline.site_name}</Badge>}{baseline.match_tags.length > 0 && <TagList tags={baseline.match_tags} max={20} />}<span>{baseline.rules.length} rules · Alerting {baseline.alerting ? 'on' : 'off'}</span></div>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" size="sm" onClick={() => { query.refetch(); qc.invalidateQueries({ queryKey: ['server-baselines', baselineId, 'checks'] }) }} disabled={query.isFetching}><RefreshCw className={cn('h-3.5 w-3.5', query.isFetching && 'animate-spin')} /> Refresh</Button>
        {canManage && <><Button variant="outline" size="sm" onClick={() => setEditing(true)}><Pencil className="h-3.5 w-3.5" /> Edit policy</Button><Button size="sm" onClick={() => evaluate.mutate()} disabled={evaluate.isPending || !baseline.enabled || baseline.rules.length === 0}><ListChecks className="h-3.5 w-3.5" />{evaluate.isPending ? 'Evaluating…' : 'Evaluate now'}</Button></>}
      </div>
    </div>
    {!baseline.enabled && <p className="rounded-md border border-border bg-surface2 p-3 text-sm text-muted">This baseline is disabled. Its rules are retained; servers are not currently evaluated against it.</p>}
    {baseline.enabled && baseline.rules.length === 0 && <p className="rounded-md border border-warning/30 bg-warning/5 p-3 text-sm text-warning">No rules configured. Add required or prohibited software rules before evaluating compliance.</p>}
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <KpiTile icon={Server} label="Servers in scope" value={summary.servers_in_scope} sub="within your visibility" />
      <KpiTile icon={CheckCircle2} label="Compliant" value={summary.compliant} sub="all current rules passed" tone="success" />
      <KpiTile icon={ShieldAlert} label="Violated" value={summary.violated} sub={`${summary.violations} failing rule checks`} tone={summary.violated ? 'danger' : 'default'} />
      <KpiTile icon={Clock3} label="Needs evaluation" value={summary.not_evaluated} sub="missing or incomplete current checks" tone={summary.not_evaluated ? 'warning' : 'default'} />
    </div>
    <p className="text-xs text-muted">Results use the latest received software inventory. Offline servers can have older findings. Re-evaluation does not collect new inventory. {summary.last_evaluated && <span title={dateTitle(summary.last_evaluated)}>Last evaluated {when(summary.last_evaluated)}.</span>}</p>
    <Tabs value={view} onValueChange={value => update({ view: value }, false)}>
    <TabsList aria-label="Baseline detail views"><TabsTrigger value="servers">Server compliance</TabsTrigger><TabsTrigger value="rules">Policy rules ({baseline.rules.length})</TabsTrigger></TabsList>
    <TabsContent value="rules"><RulesTable baseline={baseline} summary={data.rule_summary} /></TabsContent>
    <TabsContent value="servers"><Card aria-busy={query.isFetching}>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border p-4">
        <div className="flex flex-wrap gap-1" aria-label="Server compliance filters">{Object.entries(SERVER_STATES).map(([key, label]) => <Button key={key} size="sm" variant={status === key ? 'secondary' : 'ghost'} aria-pressed={status === key} onClick={() => update({ status: key === 'all' ? null : key })}>{label}</Button>)}</div>
        <div className="relative w-full sm:w-72"><Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" /><Input aria-label="Search baseline servers" placeholder="Search server name or IP…" value={search} onChange={event => setSearch(event.target.value)} className="pl-8" /></div>
      </div>
      {expanded && <div className="border-b border-border bg-info/5 px-4 py-2 text-xs text-muted">Automatic refresh is paused while server checks are open. Refresh updates findings while keeping the row order.</div>}
      <div className="overflow-x-auto"><Table className="min-w-[56rem] table-fixed">
        <colgroup><col className="w-[34%]" /><col className="w-[13%]" /><col className="w-[12%]" /><col className="w-[10%]" /><col className="w-[21%]" /><col className="w-[10%]" /></colgroup>
        <THead><Tr>{heading('server_name', 'Server')}{heading('primary_ip', 'IP address')}{heading('status', 'Compliance')}{heading('violations', 'Violations')}{heading('checked', 'Checks')}{heading('evaluated_at', 'Evaluated')}</Tr></THead>
        <TBody>{items.length === 0 ? <Tr><Td colSpan={6}><EmptyState title={q || status !== 'all' ? 'No servers match these filters' : 'No servers in scope'} hint={q || status !== 'all' ? 'Clear the search or change the compliance filter.' : 'Check the policy OS, site and tags, and your server visibility.'} /></Td></Tr> : items.map(row => <Fragment key={row.server_id}>
          <Tr className={cn(expanded === row.server_id && 'bg-primary/5')}>
            <Td><button type="button" className="flex items-center gap-2 text-left font-medium text-primary hover:underline" aria-expanded={expanded === row.server_id} aria-controls={`checks-${row.server_id}`} aria-label={`View checks for ${row.server_name}`} onClick={() => update({ server: expanded === row.server_id ? null : row.server_id }, false)}>{expanded === row.server_id ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}{row.server_name}</button><div className="ml-6 mt-1 text-xs text-muted">{row.hostname && row.hostname !== row.server_name ? `${row.hostname} · ` : ''}{row.server_status.replaceAll('_', ' ')}{row.last_seen && <span title={dateTitle(row.last_seen)}> · last seen {relativeTime(row.last_seen)}</span>}</div></Td>
            <Td className="font-mono text-xs">{row.primary_ip || '—'}</Td>
            <Td><Badge variant={variant(row.compliance_status)}>{row.compliance_status === 'not_evaluated' ? 'Needs evaluation' : LABELS[row.compliance_status]}</Badge></Td>
            <Td><span className={cn('font-medium tabular-nums', row.violations > 0 && 'text-danger')}>{row.violations}</span>{row.critical > 0 && <div className="text-xs text-danger">{row.critical} critical</div>}</Td>
            <Td className="text-xs tabular-nums"><div>{row.checked}/{baseline.rules.length} evaluated · {row.compliant} passed</div>{row.not_evaluated > 0 && <div className="text-muted">{row.not_evaluated} not evaluated</div>}</Td>
            <Td className="text-xs text-muted" title={dateTitle(row.evaluated_at)}>{when(row.evaluated_at)}</Td>
          </Tr>
          {expanded === row.server_id && <Tr className="hover:bg-transparent"><Td colSpan={6} className="bg-surface2/30 p-0"><div id={`checks-${row.server_id}`} className="space-y-3 p-4"><div className="flex flex-wrap items-center justify-between gap-2"><div><h2 className="text-base font-semibold">{row.server_name} — rule checks</h2><p className="text-xs text-muted" title={dateTitle(row.inventory_updated_at)}>{row.inventory_updated_at ? `Inventory received ${relativeTime(row.inventory_updated_at)}` : 'Inventory time unavailable'}</p></div><Button asChild size="sm" variant="ghost"><Link to={`/servers/${row.server_id}?tab=compliance`}>Open server <ExternalLink className="h-3.5 w-3.5" /></Link></Button></div><ServerChecks baselineId={baselineId} serverId={row.server_id} /></div></Td></Tr>}
        </Fragment>)}</TBody>
      </Table></div>
      <TablePager page={page} pageCount={Math.max(1, Math.ceil(data.total / pageSize))} pageSize={pageSize} total={data.total} noun="servers" setPage={value => update({ page: String(value), server: null }, false)} setPageSize={value => update({ size: String(value) })} />
    </Card></TabsContent>
    </Tabs>
    <BaselineFormDialog open={editing} onOpenChange={setEditing} baseline={baseline} />
  </div>
}

function RulesTable({ baseline, summary }: Pick<BaselineCoverageReport, 'baseline'> & { summary: BaselineCoverageReport['rule_summary'] }) {
  const stats = new Map(summary.map(row => [row.rule_id, row]))
  const paging = usePagedRows(baseline.rules)
  return <Card><div className="overflow-x-auto"><Table><THead><Tr><Th>Rule</Th><Th>Expectation</Th><Th>Severity</Th><Th>Compliant servers</Th><Th>Violated servers</Th><Th>Not evaluated</Th></Tr></THead><TBody>
    {paging.pageRows.map(rule => <Tr key={rule.id}><Td><div className="font-medium">{rule.package_match}</div><div className="text-xs text-muted">{rule.rule_type} · {rule.match_type} match</div>{rule.notes && <p className="mt-1 whitespace-pre-wrap text-xs text-text2">{rule.notes}</p>}</Td><Td className="text-xs">{expectation(rule)}</Td><Td><Badge variant={rule.severity === 'critical' ? 'danger' : rule.severity === 'warning' ? 'warning' : 'info'}>{rule.severity}</Badge></Td><Td className="tabular-nums text-success">{stats.get(rule.id)?.compliant ?? 0}</Td><Td className="tabular-nums text-danger">{stats.get(rule.id)?.violated ?? 0}</Td><Td className="tabular-nums text-muted">{stats.get(rule.id)?.not_evaluated ?? 0}</Td></Tr>)}
    {baseline.rules.length === 0 && <Tr><Td colSpan={6}><EmptyState title="No policy rules" hint="Add rules using Edit policy." /></Td></Tr>}
  </TBody></Table></div><TablePager {...paging} total={baseline.rules.length} noun="rules" /></Card>
}

function ServerChecks({ baselineId, serverId }: { baselineId: string; serverId: string }) {
  const [filter, setFilter] = useState('all')
  const [search, setSearch] = useState('')
  const query = useQuery<{ items: BaselineRuleCheck[] }>({
    queryKey: ['server-baselines', baselineId, 'checks', serverId],
    queryFn: async () => (await api.get(`/server-baselines/${baselineId}/servers/${serverId}/checks`)).data,
    refetchOnWindowFocus: false,
  })
  const rows = useMemo(() => (query.data?.items || []).filter(row => (filter === 'all' || (filter === 'violated' ? !['compliant', 'not_evaluated'].includes(row.status) : row.status === filter)) && `${row.package_match} ${row.found_package || ''} ${row.found_version || ''}`.toLowerCase().includes(search.trim().toLowerCase())), [query.data, filter, search])
  const paging = usePagedRows(rows)
  if (query.isError) return <QueryError error={query.error} onRetry={() => query.refetch()} />
  if (query.isLoading) return <Skeleton className="h-36 w-full" />
  return <div className="space-y-3">
    <div className="flex flex-wrap items-center gap-2"><select aria-label="Filter rule checks" className="rounded-md border border-border bg-surface p-2 text-xs" value={filter} onChange={event => { setFilter(event.target.value); paging.setPage(1) }}><option value="all">All checks</option><option value="violated">Violations</option>{(['compliant', 'missing', 'outdated', 'prohibited', 'not_evaluated'] as const).map(key => <option key={key} value={key}>{LABELS[key]}</option>)}</select><Input aria-label="Search rule checks" placeholder="Search software or version…" className="max-w-sm" value={search} onChange={event => { setSearch(event.target.value); paging.setPage(1) }} /></div>
    <div className="overflow-x-auto rounded-md border border-border bg-surface"><Table><THead><Tr><Th>Status</Th><Th>Rule / expected</Th><Th>Found software</Th><Th>Severity</Th><Th>Failing since</Th><Th>Evaluated</Th></Tr></THead><TBody>
      {paging.pageRows.map(row => <Tr key={row.rule_id}><Td><Badge variant={variant(row.status)}>{LABELS[row.status]}</Badge></Td><Td><div className="font-medium">{row.package_match}</div><div className="text-xs text-muted">{expectation(row)}</div><div className="text-xs text-muted">{row.match_type} match</div>{row.notes && <p className="mt-1 whitespace-pre-wrap text-xs text-text2">{row.notes}</p>}</Td><Td className="text-xs">{row.found_package ? <><div>{row.found_package}</div><div className="font-mono text-muted">{row.found_version || 'Version not reported'}</div></> : row.status === 'not_evaluated' ? 'No current result' : row.rule_type === 'prohibited' && row.status === 'compliant' ? 'Not installed — requirement met' : 'Not found'}</Td><Td><Badge variant={row.severity === 'critical' ? 'danger' : row.severity === 'warning' ? 'warning' : 'info'}>{row.severity}</Badge></Td><Td className="text-xs text-muted" title={dateTitle(row.first_failed_at)}>{row.first_failed_at ? relativeTime(row.first_failed_at) : '—'}</Td><Td className="text-xs text-muted" title={dateTitle(row.evaluated_at)}>{when(row.evaluated_at)}</Td></Tr>)}
      {rows.length === 0 && <Tr><Td colSpan={6}><EmptyState title="No checks match these filters" /></Td></Tr>}
    </TBody></Table><TablePager {...paging} total={rows.length} noun="checks" /></div>
  </div>
}
