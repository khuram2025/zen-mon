import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowRight, Clock, Download, DownloadCloud, FileCode, KeyRound, ListChecks, RefreshCw, Search, Settings2, X,
} from 'lucide-react'
import { useCan } from '@/stores/auth'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { NcmHealth } from '@/components/ncm/NcmReadiness'
import { Ago, EmptyState, Kpi, Pagination, StaleBanner, StateBadge, ToneIcon } from '@/components/ncm/ui'
import {
  STATES, RISK_ORDER, TONE_BG, categoryLabel, download, ncmError, pct, scheduleSummary, stateOf, toCsv, type Tone,
} from '@/components/ncm/model'
import { AssignProfileDialog, BulkBackupDialog, BulkSettingsDialog, SelectionReviewDialog } from '@/components/ncm/BulkDialogs'
import { ProfilesDialog } from '@/components/ncm/ProfilesDialog'
import { ComplianceWorkspace } from '@/components/ncm/ComplianceWorkspace'
import { JobsWorkspace } from '@/components/ncm/OperationsWorkspace'
import { AuditWorkspace } from '@/components/ncm/AuditWorkspace'
import { ArchiveSearch, CrossDeviceCompare, SupportMatrix } from '@/components/ncm/ArchiveTools'

const PAGE_SIZES = [25, 50, 100, 200]
const FACET_KEYS = ['status', 'vendor', 'device_type', 'location'] as const
type FacetKey = typeof FACET_KEYS[number]
const FACET_LABEL: Record<FacetKey, string> = { status: 'Protection', vendor: 'Vendor', device_type: 'Type', location: 'Site' }
const FLAG_LABEL: Record<string, string> = {
  review: 'Needs scope review', unsupported: 'No collector', overdue: 'Schedule overdue', incident: 'Open incident',
  compliance: 'Failing compliance', never_captured: 'Never captured', outside_scope: 'Enrolled outside scope',
}
const TABS = ['overview', 'devices', 'jobs', 'compliance', 'archive', 'audit', 'collectors'] as const

function useUrlState() {
  const [params, setParams] = useSearchParams()
  const get = (k: string) => params.get(k) || ''
  const list = (k: string) => (params.get(k) || '').split(',').filter(Boolean)
  const update = (patch: Record<string, string | string[] | null>, resetPage = true) => {
    const next = new URLSearchParams(params)
    for (const [k, v] of Object.entries(patch)) {
      const value = Array.isArray(v) ? v.join(',') : v
      if (value) next.set(k, value); else next.delete(k)
    }
    if (resetPage && !('page' in patch)) next.delete('page')
    setParams(next, { replace: true })
  }
  return { params, get, list, update }
}

export function NcmPage() {
  const can = useCan()
  const qc = useQueryClient()
  const url = useUrlState()
  const tab = (TABS as readonly string[]).includes(url.get('tab')) ? url.get('tab') : 'overview'
  const [profilesOpen, setProfilesOpen] = useState(false)
  const refreshAll = () => qc.invalidateQueries({ queryKey: ['ncm'] })

  const summary = useQuery<any>({ queryKey: ['ncm', 'overview', 'summary'], queryFn: async () => (await api.get('/ncm/overview', { params: { page_size: 1 } })).data, refetchInterval: 30000 })
  const runScheduled = useMutation({
    mutationFn: async () => (await api.post('/ncm/run-scheduled', null, { timeout: 120000 })).data,
    onSuccess: (d: any) => {
      const text = `${d.queued} queued, ${d.already_active} already active, ${d.blocked || 0} blocked (${d.due} due)`
      ;(d.blocked ? toast.warning : toast.success)('Due schedules processed', text)
      refreshAll()
    },
    onError: (e: any) => toast.error('Could not process schedules', ncmError(e)),
  })

  return (
    <div className="min-w-0 space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <FileCode className="h-5 w-5 shrink-0 text-primary" aria-hidden /> Network Configuration &amp; Compliance
          </h1>
          <p className="text-sm text-muted">What is protected, what is at risk, what changed and what needs action.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => runScheduled.mutate()} disabled={!can('ncm.manage') || runScheduled.isPending}
            title="Queue backups for every schedule that is due now (the scheduler also does this every minute)"><Clock className="h-3.5 w-3.5" /> Run due schedules</Button>
          <Button variant="outline" size="sm" disabled={!can('ncm.credentials')} onClick={() => setProfilesOpen(true)}><KeyRound className="h-3.5 w-3.5" /> Connection profiles</Button>
          <Button variant="outline" size="sm" onClick={refreshAll} aria-label="Refresh all NCCM data"><RefreshCw className={cn('h-3.5 w-3.5', qc.isFetching({ queryKey: ['ncm'] }) > 0 && 'animate-spin')} /> Refresh</Button>
        </div>
      </div>

      <NcmHealth />

      <Tabs value={tab} onValueChange={(v) => url.update({ tab: v === 'overview' ? null : v }, false)}>
        <div className="-mx-1 overflow-x-auto px-1">
          <TabsList>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="devices">Devices</TabsTrigger>
            <TabsTrigger value="jobs">Jobs &amp; delivery</TabsTrigger>
            <TabsTrigger value="compliance">Compliance</TabsTrigger>
            <TabsTrigger value="archive">Search &amp; compare</TabsTrigger>
            {(can('ncm.manage') || can('audit.view')) && <TabsTrigger value="audit">Audit</TabsTrigger>}
            <TabsTrigger value="collectors">Collectors</TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="overview"><ManagementOverview query={summary} goDevices={(patch) => url.update({ tab: 'devices', ...patch })} /></TabsContent>
        <TabsContent value="devices"><DevicesWorkspace url={url} /></TabsContent>
        <TabsContent value="jobs"><JobsWorkspace /></TabsContent>
        <TabsContent value="compliance"><ComplianceWorkspace /></TabsContent>
        <TabsContent value="archive" className="space-y-4"><ArchiveSearch /><CrossDeviceCompare /></TabsContent>
        <TabsContent value="audit"><AuditWorkspace /></TabsContent>
        <TabsContent value="collectors"><SupportMatrix /></TabsContent>
      </Tabs>

      <ProfilesDialog open={profilesOpen} onOpenChange={setProfilesOpen} />
    </div>
  )
}

// --------------------------------------------------------------------------- //
// Management overview
// --------------------------------------------------------------------------- //
function ManagementOverview({ query, goDevices }: { query: any; goDevices: (patch: Record<string, string | null>) => void }) {
  const { data, isPending, isError, refetch, isFetching, dataUpdatedAt } = query
  const outcomes = useQuery<any>({ queryKey: ['ncm', 'outcomes', 14], queryFn: async () => (await api.get('/ncm/outcomes', { params: { days: 14 } })).data, refetchInterval: 60000 })
  const health = useQuery<any>({ queryKey: ['ncm', 'health'], queryFn: async () => (await api.get('/ncm/health')).data, refetchInterval: 30000 })
  if (isError && !data) {
    return (
      <div role="alert" className="space-y-3 rounded-md border border-danger/40 bg-danger/5 p-4">
        <h2 className="font-semibold">Protection status unavailable</h2>
        <p className="text-sm">The NCCM overview could not be loaded. Device counts and protection are unknown — this is not the same as “no devices”.</p>
        <Button onClick={() => refetch()}>Retry</Button>
      </div>
    )
  }
  if (isPending) return <p className="text-sm text-muted">Loading protection status…</p>
  const eligible = data.eligible as number
  const fresh = data.fresh as number
  const a = data.attention
  const actions: { key: string; count: number; label: string; hint: string; tone: Tone; go: Record<string, string | null> }[] = [
    { key: 'failed', count: a.failed, label: 'Backups failing', hint: 'Last attempt failed; open for the cause and fix.', tone: 'danger', go: { status: 'failed' } },
    { key: 'blocked', count: a.blocked, label: 'Blocked by a missing prerequisite', hint: 'Scheduled backups are skipped until setup is fixed (e.g. SSH trust).', tone: 'danger', go: { status: 'blocked' } },
    { key: 'never', count: a.never_captured, label: 'Enrolled but never captured', hint: 'Run the first backup to prove access.', tone: 'warning', go: { flag: 'never_captured' } },
    { key: 'unenrolled', count: a.eligible_not_enrolled, label: 'Eligible but not enrolled', hint: `In scope with no backup configured${a.unsupported_eligible ? ` (${a.unsupported_eligible} of all eligible have no collector yet)` : ''}.`, tone: 'warning', go: { status: 'unconfigured' } },
    { key: 'stale', count: a.stale, label: 'Stale', hint: 'Validated copy older than the freshness limit.', tone: 'warning', go: { status: 'stale' } },
    { key: 'overdue', count: a.overdue, label: 'Schedules overdue', hint: 'A due backup has not started for 15+ minutes.', tone: 'warning', go: { flag: 'overdue' } },
    { key: 'unsupported', count: a.unsupported_eligible, label: 'Eligible without a collector', hint: 'No implemented driver (e.g. F5); they stay in the coverage denominator.', tone: 'warning', go: { flag: 'unsupported' } },
    { key: 'review', count: a.eligibility_review, label: 'Need scope review', hint: 'Network vendors whose inventory type/address is outside automatic scope.', tone: 'neutral', go: { flag: 'review' } },
    { key: 'compliance', count: a.compliance_failing, label: 'Failing compliance checks', hint: 'At least one failed rule on the latest validated configuration.', tone: 'warning', go: { flag: 'compliance' } },
  ]
  const states = RISK_ORDER.filter((k) => data.eligible_states?.[k]).map((k) => ({ key: k, n: data.eligible_states[k], ...STATES[k] }))
  return (
    <div className="space-y-4">
      {isError && <StaleBanner what="protection status" updatedAt={dataUpdatedAt} onRetry={() => refetch()} retrying={isFetching} />}
      <Card>
        <CardContent className="space-y-4 pt-5">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <div className="text-sm text-muted">Protected devices</div>
              <div className={cn('text-4xl font-semibold tabular-nums', !eligible ? 'text-muted' : fresh === eligible ? 'text-success' : fresh === 0 ? 'text-danger' : 'text-warning')}>
                {fresh} <span className="text-2xl text-muted">of {eligible}</span> <span className="text-2xl">{pct(fresh, eligible)}</span>
              </div>
              <p className="mt-1 max-w-2xl text-sm text-muted">
                {eligible
                  ? fresh === 0
                    ? 'No eligible device has a fresh, validated configuration backup. Recovery is not currently possible from NCCM.'
                    : `${eligible - fresh} eligible device${eligible - fresh === 1 ? ' is' : 's are'} not protected.`
                  : 'No device is in the automatic backup scope; coverage cannot be measured.'}{' '}
                Protected = fresh, complete and format-validated for every required type. {data.restore_tested} of {eligible} have a passed restore test.
              </p>
            </div>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
              <div><dt className="text-xs text-muted">Inventory</dt><dd className="font-semibold tabular-nums">{data.total_devices}</dd></div>
              <div><dt className="text-xs text-muted">Eligible (in scope)</dt><dd className="font-semibold tabular-nums">{eligible}</dd></div>
              <div><dt className="text-xs text-muted">Eligible enrolled</dt><dd className="font-semibold tabular-nums">{data.eligible_enrolled} of {eligible}</dd></div>
              <div><dt className="text-xs text-muted">Enrolled outside scope</dt><dd className="font-semibold tabular-nums"><button type="button" className="underline-offset-2 hover:underline" onClick={() => goDevices({ flag: 'outside_scope' })}>{data.enrolled_outside_scope}</button></dd></div>
            </dl>
          </div>
          {eligible > 0 && (
            <div>
              <div className="flex h-4 w-full overflow-hidden rounded-full bg-surface2" role="img" aria-label={states.map((s) => `${s.label} ${s.n}`).join(', ')}>
                {states.map((s) => (
                  <button key={s.key} type="button" title={`${s.label}: ${s.n}`} aria-label={`Show ${s.label} devices (${s.n})`} onClick={() => goDevices({ status: s.key })}
                    className={cn('h-full border-r border-surface last:border-0', TONE_BG[s.tone])} style={{ width: `${(s.n / eligible) * 100}%` }} />
                ))}
              </div>
              <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
                {states.map((s) => (
                  <li key={s.key}><button type="button" onClick={() => goDevices({ status: s.key })} className="inline-flex items-center gap-1 hover:underline"><ToneIcon tone={s.tone} />{s.label} <strong className="tabular-nums">{s.n}</strong></button></li>
                ))}
              </ul>
            </div>
          )}
        </CardContent>
      </Card>

      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Card className="min-w-0">
          <CardContent className="space-y-2 pt-4">
            <h2 className="font-semibold">Needs action</h2>
            <p className="text-xs text-muted">Counts can overlap (a failing device may also be overdue). Select a row to see the devices.</p>
            <ul className="divide-y divide-border">
              {actions.map((x) => (
                <li key={x.key}>
                  <button type="button" onClick={() => goDevices(x.go)} disabled={!x.count}
                    className="flex w-full items-center gap-3 px-1 py-2 text-left hover:bg-surface2/60 disabled:cursor-default disabled:hover:bg-transparent">
                    <ToneIcon tone={x.count ? x.tone : 'success'} className="h-4 w-4" />
                    <span className="min-w-0 flex-1"><span className="block text-sm font-medium">{x.label}</span><span className="block text-xs text-muted">{x.hint}</span></span>
                    <span className={cn('text-lg font-semibold tabular-nums', x.count ? '' : 'text-muted')}>{x.count}</span>
                    {x.count > 0 && <ArrowRight className="h-4 w-4 text-muted" aria-hidden />}
                  </button>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
        <div className="min-w-0 space-y-4">
          <Card><CardContent className="pt-4"><OutcomeTrend query={outcomes} /></CardContent></Card>
          <Card>
            <CardContent className="space-y-2 pt-4">
              <h2 className="font-semibold">Evidence quality</h2>
              <ul className="space-y-1.5 text-sm">
                <li className="flex items-center gap-2"><ToneIcon tone={health.data?.time_sync === 'synchronized' ? 'success' : health.data?.time_sync === 'unsynchronized' ? 'danger' : 'warning'} />Appliance clock {health.data?.time_sync === 'synchronized' ? 'NTP-synchronized' : health.data?.time_sync === 'unsynchronized' ? 'NOT synchronized — timestamps may be wrong' : 'sync state unknown'}</li>
                <li className="flex items-center gap-2"><ToneIcon tone="info" />Status refreshed <Ago iso={data.server_time} /></li>
                <li className="flex items-center gap-2"><ToneIcon tone={data.restore_tested ? 'success' : 'warning'} />{data.restore_tested} eligible device{data.restore_tested === 1 ? '' : 's'} restore-tested</li>
                {data.legacy_plaintext > 0 && <li className="flex items-center gap-2"><ToneIcon tone="warning" />{data.legacy_plaintext} stored version(s) awaiting archive encryption</li>}
              </ul>
            </CardContent>
          </Card>
        </div>
      </div>

      <div className="grid min-w-0 gap-4 lg:grid-cols-2">
        <CoverageTable title="Coverage by vendor" rows={data.coverage_by?.vendor || []} onPick={(name) => goDevices({ vendor: name })} />
        <CoverageTable title="Coverage by site" rows={data.coverage_by?.location || []} onPick={(name) => goDevices({ location: name })} />
      </div>
    </div>
  )
}

function OutcomeTrend({ query }: { query: any }) {
  const { data, isPending, isError, refetch } = query
  if (isError) return <div role="alert" className="text-sm text-warning">Backup outcomes could not be loaded. <Button size="sm" variant="outline" onClick={() => refetch()}>Retry</Button></div>
  if (isPending) return <p className="text-sm text-muted">Loading outcomes…</p>
  const points: any[] = data.data
  const max = Math.max(1, ...points.map((p) => p.succeeded + p.failed))
  const totals = points.reduce((t, p) => ({ ok: t.ok + p.succeeded, bad: t.bad + p.failed }), { ok: 0, bad: 0 })
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-semibold">Backup outcomes · last {data.days} days</h2>
        <span className="text-xs text-muted"><span className="text-success">{totals.ok} validated</span> · <span className="text-danger">{totals.bad} failed attempts</span></span>
      </div>
      {!totals.ok && !totals.bad ? <p className="text-sm text-muted">No backup attempts in this period.</p> : (
        <>
          <div className="flex h-28 items-end gap-1" role="img" aria-label={`Daily outcomes: ${points.map((p) => `${p.day} ${p.succeeded} validated ${p.failed} failed`).join('; ')}`}>
            {points.map((p) => (
              <div key={p.day} className="flex h-full flex-1 flex-col justify-end" title={`${p.day}: ${p.succeeded} validated, ${p.failed} failed (UTC day)`}>
                <div className="w-full rounded-t-sm bg-danger/80" style={{ height: `${(p.failed / max) * 100}%` }} />
                <div className="w-full bg-success/80" style={{ height: `${(p.succeeded / max) * 100}%` }} />
              </div>
            ))}
          </div>
          <div className="flex justify-between text-[11px] text-muted"><span>{points[0]?.day}</span><span>{points[points.length - 1]?.day} (UTC)</span></div>
        </>
      )}
    </div>
  )
}

function CoverageTable({ title, rows, onPick }: { title: string; rows: any[]; onPick: (name: string) => void }) {
  return (
    <Card className="min-w-0">
      <CardContent className="space-y-2 pt-4">
        <h2 className="font-semibold">{title}</h2>
        {!rows.length ? <p className="text-sm text-muted">No eligible devices.</p> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead><tr className="text-left text-xs text-muted"><th className="py-1 pr-2 font-medium">Name</th><th className="py-1 pr-2 text-right font-medium">Protected / eligible</th><th className="w-2/5 py-1 font-medium"><span className="sr-only">Coverage</span></th></tr></thead>
              <tbody>
                {rows.slice(0, 10).map((r) => (
                  <tr key={r.name} className="border-t border-border">
                    <td className="py-1.5 pr-2"><button type="button" className="text-left hover:underline" onClick={() => onPick(r.name)}>{r.name}</button>{r.unsupported > 0 && <span className="ml-1 text-xs text-warning">({r.unsupported} no collector)</span>}</td>
                    <td className="py-1.5 pr-2 text-right tabular-nums">{r.protected} / {r.eligible} <span className="text-muted">({pct(r.protected, r.eligible)})</span></td>
                    <td className="py-1.5"><div className="h-2 overflow-hidden rounded-full bg-surface2" aria-hidden><div className={cn('h-full', r.protected === r.eligible ? 'bg-success' : r.protected ? 'bg-warning' : 'bg-danger')} style={{ width: `${(r.protected / r.eligible) * 100}%` }} /></div></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

// --------------------------------------------------------------------------- //
// Devices workspace
// --------------------------------------------------------------------------- //
function DevicesWorkspace({ url }: { url: ReturnType<typeof useUrlState> }) {
  const can = useCan()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const q = url.get('q')
  const [searchText, setSearchText] = useState(q)
  useEffect(() => { setSearchText(q) }, [q])
  useEffect(() => {
    const t = setTimeout(() => { if (searchText !== q) url.update({ q: searchText || null }) }, 350)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchText])
  const filters: Record<FacetKey, string[]> = { status: url.list('status'), vendor: url.list('vendor'), device_type: url.list('device_type'), location: url.list('location') }
  const flag = url.get('flag')
  const attention = url.get('attention') === '1'
  const sort = url.get('sort') || 'risk'
  const pageSize = PAGE_SIZES.includes(Number(url.get('size'))) ? Number(url.get('size')) : 50
  const page = Math.max(1, Number(url.get('page')) || 1)
  const params = {
    q: q || undefined, status: filters.status.join(',') || undefined, vendor: filters.vendor.join(',') || undefined,
    device_type: filters.device_type.join(',') || undefined, location: filters.location.join(',') || undefined,
    flag: flag || undefined, attention: attention || undefined, sort, page, page_size: pageSize,
  }
  const overview = useQuery<any>({
    queryKey: ['ncm', 'overview', 'devices', params],
    queryFn: async () => (await api.get('/ncm/overview', { params })).data,
    refetchInterval: 30000, placeholderData: keepPreviousData,
  })
  const { data, isPending, isError, refetch, isFetching, dataUpdatedAt } = overview
  const rows: any[] = data?.data || []
  const pageCount = data ? Math.max(1, Math.ceil(data.filtered_total / pageSize)) : 1
  // B20: the server clamps the page; mirror it so the URL never points past the end.
  useEffect(() => { if (data && data.page !== page) url.update({ page: data.page > 1 ? String(data.page) : null }, false) }, [data?.page]) // eslint-disable-line react-hooks/exhaustive-deps

  // ---- selection (B11): ids with last-known rows, reconciled with the server ----
  const [selected, setSelected] = useState<Map<string, any>>(new Map())
  const ids = useMemo(() => [...selected.keys()].sort(), [selected])
  const selectionCheck = useQuery<any>({
    queryKey: ['ncm', 'overview', 'selection', ids],
    queryFn: async () => (await api.get('/ncm/overview', { params: { ids: ids.join(','), page_size: 1000 } })).data,
    enabled: ids.length > 0, refetchInterval: 30000,
  })
  useEffect(() => {
    if (!selectionCheck.data) return
    const fresh = new Map<string, any>(selectionCheck.data.data.map((d: any) => [d.device_id, d]))
    const gone = ids.filter((id) => !fresh.has(id))
    setSelected((cur) => {
      const next = new Map<string, any>()
      for (const id of cur.keys()) if (fresh.has(id)) next.set(id, fresh.get(id))
      return next
    })
    if (gone.length) toast.info(`${gone.length} selected device${gone.length === 1 ? ' is' : 's are'} no longer available`, 'Removed from the selection (deleted or outside your scope).')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectionCheck.data])
  const visibleIds = new Set(rows.map((d) => d.device_id))
  const hiddenIds = new Set(ids.filter((id) => !visibleIds.has(id)))
  const selectedDevices = [...selected.values()]
  const allPage = rows.length > 0 && rows.every((d) => selected.has(d.device_id))
  const toggle = (d: any) => setSelected((cur) => { const n = new Map(cur); n.has(d.device_id) ? n.delete(d.device_id) : n.set(d.device_id, d); return n })
  const togglePage = () => setSelected((cur) => { const n = new Map(cur); rows.forEach((d) => (allPage ? n.delete(d.device_id) : n.set(d.device_id, d))); return n })
  const [dialog, setDialog] = useState<'' | 'review' | 'assign' | 'settings' | 'backup'>('')
  const creds = useQuery<any>({ queryKey: ['ncm', 'credentials'], queryFn: async () => (await api.get('/ncm/credentials')).data, enabled: dialog === 'assign' })
  const platforms = useQuery<any>({ queryKey: ['ncm', 'platforms'], queryFn: async () => (await api.get('/ncm/platforms')).data, enabled: dialog === 'assign' })
  const stale = isError && !!data
  const afterBulk = () => qc.invalidateQueries({ queryKey: ['ncm'] })

  const activeChips = [
    ...FACET_KEYS.flatMap((k) => filters[k].map((v) => ({ key: k, value: v, label: `${FACET_LABEL[k]}: ${k === 'status' ? STATES[v]?.label || v : v}` }))),
    ...(flag ? [{ key: 'flag', value: flag, label: FLAG_LABEL[flag] || flag }] : []),
    ...(attention ? [{ key: 'attention', value: '1', label: 'Needs attention' }] : []),
  ]
  const removeChip = (c: { key: string; value: string }) => {
    if (c.key === 'flag' || c.key === 'attention') url.update({ [c.key]: null })
    else url.update({ [c.key]: filters[c.key as FacetKey].filter((v) => v !== c.value) })
  }
  const toggleFacet = (k: FacetKey, v: string) => url.update({ [k]: filters[k].includes(v) ? filters[k].filter((x) => x !== v) : [...filters[k], v] })

  const exportCsv = async () => {
    try {
      const all = (await api.get('/ncm/overview', { params: { ...params, page: 1, page_size: 1000 } })).data.data
      download(`nccm-devices-${new Date().toISOString().slice(0, 10)}.csv`, toCsv([
        ['Hostname', 'IP', 'Vendor', 'Type', 'Site', 'Coverage', 'Protection', 'Latest validated capture (UTC)', 'Last attempt (UTC)', 'Last failure', 'Schedule', 'Next due (UTC)', 'Collector'],
        ...all.map((d: any) => [d.hostname, d.ip, d.vendor, d.device_type, d.location, d.coverage, stateOf(d).label, d.validated_at, d.last_attempt_at,
          d.last_failure_category ? categoryLabel(d.last_failure_category) : '', d.enrolled ? scheduleSummary(d) : '', d.next_due, d.platform_label || d.support?.state]),
      ]), 'text/csv')
    } catch (e: any) { toast.error('Export failed', ncmError(e)) }
  }

  if (isError && !data) {
    return <div role="alert" className="space-y-3 rounded-md border border-danger/40 bg-danger/5 p-4"><p className="text-sm">The device list could not be loaded. Nothing is assumed about device protection until it loads.</p><Button onClick={() => refetch()}>Retry</Button></div>
  }
  return (
    <div className="min-w-0 space-y-3">
      {stale && <StaleBanner what="the device list" updatedAt={dataUpdatedAt} onRetry={() => refetch()} retrying={isFetching} />}
      <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,1fr)_260px] lg:items-start">
        <Card className="min-w-0">
          <CardContent className="min-w-0 space-y-3 pt-4">
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative min-w-[200px] flex-1">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" aria-hidden />
                <Input className="pl-8" aria-label="Search devices" placeholder="Search hostname, IP, vendor, site, profile…" value={searchText} onChange={(e) => setSearchText(e.target.value)} />
              </div>
              <Button size="sm" variant={attention ? 'default' : 'outline'} aria-pressed={attention} onClick={() => url.update({ attention: attention ? null : '1' })}>Needs attention</Button>
              <Select value={sort} onValueChange={(v) => url.update({ sort: v === 'risk' ? null : v })}>
                <SelectTrigger aria-label="Sort devices" className="h-8 w-[150px]"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="risk">Sort: highest risk</SelectItem><SelectItem value="hostname">Sort: hostname</SelectItem><SelectItem value="last_capture">Sort: latest capture</SelectItem><SelectItem value="next_due">Sort: next due</SelectItem></SelectContent>
              </Select>
              <Button size="sm" variant="outline" onClick={exportCsv}><Download className="h-3.5 w-3.5" /> CSV</Button>
            </div>
            {activeChips.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5">
                {activeChips.map((c) => (
                  <button key={`${c.key}:${c.value}`} type="button" onClick={() => removeChip(c)} aria-label={`Remove filter ${c.label}`}
                    className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2.5 py-1 text-xs text-primary hover:bg-primary/20">{c.label}<X className="h-3 w-3" aria-hidden /></button>
                ))}
                <Button size="sm" variant="link" className="h-auto px-1 text-xs" onClick={() => url.update({ q: null, status: null, vendor: null, device_type: null, location: null, flag: null, attention: null })}>Reset all</Button>
              </div>
            )}

            {selected.size > 0 && (
              <div className="flex flex-wrap items-center gap-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-xs" role="region" aria-label="Bulk actions">
                <span className="font-medium text-primary">{selected.size} selected</span>
                {hiddenIds.size > 0 && <span className="text-warning">({hiddenIds.size} hidden by filters or on other pages)</span>}
                <Button size="sm" variant="ghost" onClick={() => setDialog('review')}><ListChecks className="h-3.5 w-3.5" /> Review selection</Button>
                <span className="flex-1" />
                <Button size="sm" variant="outline" disabled={!can('ncm.manage') || stale} onClick={() => setDialog('assign')}><KeyRound className="h-3.5 w-3.5" /> Assign profile</Button>
                <Button size="sm" variant="outline" disabled={!can('ncm.manage') || stale || !selectedDevices.some((d) => d.enrolled)} onClick={() => setDialog('settings')}><Clock className="h-3.5 w-3.5" /> Backup settings</Button>
                <Button size="sm" variant="outline" disabled={!can('ncm.manage') || stale || !selectedDevices.some((d) => d.enrolled)} onClick={() => setDialog('backup')}><DownloadCloud className="h-3.5 w-3.5" /> Run backup now</Button>
                <Button size="sm" variant="ghost" onClick={() => setSelected(new Map())} aria-label="Clear selection"><X className="h-3.5 w-3.5" /> Clear</Button>
              </div>
            )}

            {isPending ? <p className="py-8 text-center text-sm text-muted">Loading devices…</p> : !rows.length ? (
              <EmptyState>{data.total_devices ? 'No devices match these filters.' : 'No devices in your inventory scope.'}</EmptyState>
            ) : (
              <>
                {/* Cards on narrow screens, table from md up (B18). */}
                <ul className="space-y-2 md:hidden">
                  {rows.map((d) => (
                    <li key={d.device_id} className={cn('rounded-md border p-3', selected.has(d.device_id) ? 'border-primary/50 bg-primary/5' : 'border-border')}>
                      <div className="flex items-start gap-2">
                        <input type="checkbox" className="mt-1 h-4 w-4 accent-primary" aria-label={`Select ${d.hostname}`} checked={selected.has(d.device_id)} onChange={() => toggle(d)} />
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center justify-between gap-2"><Link to={`/ncm/${d.device_id}`} className="truncate font-medium hover:underline">{d.hostname}</Link><StateBadge device={d} /></div>
                          <div className="font-mono text-xs text-muted">{d.ip}</div>
                          <div className="mt-1 text-xs text-muted"><ReasonLine d={d} /></div>
                          <div className="mt-1 flex flex-wrap gap-x-3 text-xs"><span>Valid: <Ago iso={d.validated_at} /></span><span>{d.enrolled ? scheduleSummary(d) : 'Not enrolled'}</span></div>
                        </div>
                      </div>
                    </li>
                  ))}
                </ul>
                <div className="hidden overflow-hidden rounded-md border border-border md:block">
                  <Table>
                    <THead className="bg-surface2/50">
                      <Tr>
                        <Th className="w-8"><input type="checkbox" className="h-4 w-4 cursor-pointer accent-primary" checked={allPage} onChange={togglePage} aria-label="Select all devices on this page" /></Th>
                        <Th>Device</Th><Th>Site</Th><Th>Protection</Th><Th>Latest valid capture</Th><Th>Last attempt</Th><Th>Schedule</Th><Th>Collector</Th><Th><span className="sr-only">Actions</span></Th>
                      </Tr>
                    </THead>
                    <TBody>
                      {rows.map((d) => (
                        <Tr key={d.device_id} className={cn('cursor-pointer', selected.has(d.device_id) && 'bg-primary/5')} onClick={() => navigate(`/ncm/${d.device_id}`)}>
                          <Td onClick={(e) => e.stopPropagation()}><input type="checkbox" className="h-4 w-4 cursor-pointer accent-primary" aria-label={`Select ${d.hostname}`} checked={selected.has(d.device_id)} onChange={() => toggle(d)} /></Td>
                          <Td><div className="font-medium">{d.hostname}</div><div className="font-mono text-xs text-muted">{d.ip}</div></Td>
                          <Td className="text-sm">{d.location || <span className="text-muted">—</span>}<div className="text-xs text-muted">{[d.vendor, d.device_type].filter(Boolean).join(' · ')}</div></Td>
                          <Td><StateBadge device={d} />{d.coverage === 'excluded' && d.enrolled && <Badge variant="outline" className="ml-1">outside scope</Badge>}<div className="mt-1 max-w-64 text-xs text-muted"><ReasonLine d={d} /></div></Td>
                          <Td className="text-sm"><Ago iso={d.validated_at} /></Td>
                          <Td className="text-sm">{d.last_attempt_at ? <><Ago iso={d.last_attempt_at} /><div className={cn('text-xs', d.last_status === 'success' ? 'text-success' : d.last_status ? 'text-danger' : 'text-muted')}>{d.last_status === 'success' ? 'Succeeded' : d.last_failure_category ? categoryLabel(d.last_failure_category) : d.last_status || ''}</div></> : <span className="text-muted">never</span>}</Td>
                          <Td className="text-sm">{d.enrolled ? <><div>{scheduleSummary(d)}</div>{d.next_due && d.ncm_enabled && <div className={cn('text-xs', d.overdue ? 'text-warning' : 'text-muted')}>{d.overdue ? 'Overdue since ' : 'Next '}<Ago iso={d.next_due} /></div>}</> : <span className="text-muted">Not enrolled</span>}</Td>
                          <Td className="text-xs">{d.platform_label || (d.support?.state === 'unsupported' ? <span className="text-warning">No collector</span> : <span className="text-muted">Auto-detect</span>)}</Td>
                          <Td onClick={(e) => e.stopPropagation()}><Button variant="ghost" size="sm" asChild><Link to={`/ncm/${d.device_id}`} aria-label={`Open ${d.hostname}`}><Settings2 className="h-3.5 w-3.5" /> Details</Link></Button></Td>
                        </Tr>
                      ))}
                    </TBody>
                  </Table>
                </div>
              </>
            )}
            {data && (
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Pagination page={data.page} pageCount={pageCount} total={data.filtered_total} pageSize={pageSize} label="devices" onPage={(p) => url.update({ page: p > 1 ? String(p) : null }, false)} />
                <Select value={String(pageSize)} onValueChange={(v) => url.update({ size: v === '50' ? null : v })}>
                  <SelectTrigger aria-label="Devices per page" className="h-8 w-[120px]"><SelectValue /></SelectTrigger>
                  <SelectContent>{PAGE_SIZES.map((n) => <SelectItem key={n} value={String(n)}>{n} per page</SelectItem>)}</SelectContent>
                </Select>
              </div>
            )}
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardContent className="space-y-4 pt-4">
            <div><h2 className="text-sm font-semibold">Breakdown</h2><p className="text-xs text-muted">Entire inventory in your scope ({data?.total_devices ?? '—'} devices); counts do not change with filters. Select values to filter; several can be combined.</p></div>
            {data && FACET_KEYS.map((k) => {
              const items: [string, number][] = (data.facets?.[k === 'device_type' ? 'type' : k] || [])
              if (!items.length) return null
              const max = Math.max(...items.map((i) => i[1]), 1)
              return (
                <div key={k} className="space-y-1">
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">{FACET_LABEL[k]}</h3>
                  {items.slice(0, 12).map(([value, n]) => {
                    const on = filters[k].includes(value)
                    const tone: Tone = k === 'status' ? (STATES[value]?.tone || 'neutral') : 'info'
                    return (
                      <button key={value} type="button" aria-pressed={on} onClick={() => toggleFacet(k, value)}
                        className={cn('w-full rounded px-2 py-1 text-left text-xs transition-colors', on ? 'bg-primary/10 ring-1 ring-primary/40' : 'hover:bg-surface2')}>
                        <div className="flex items-center justify-between gap-2"><span className="flex min-w-0 items-center gap-1 truncate">{k === 'status' && <ToneIcon tone={tone} />}{k === 'status' ? STATES[value]?.label || value : value}</span><span className="tabular-nums text-muted">{n}</span></div>
                        <div className="mt-1 h-1 overflow-hidden rounded-full bg-surface2"><div className={cn('h-full rounded-full', k === 'status' ? TONE_BG[tone] : 'bg-primary/50')} style={{ width: `${(n / max) * 100}%` }} /></div>
                      </button>
                    )
                  })}
                </div>
              )
            })}
          </CardContent>
        </Card>
      </div>

      <SelectionReviewDialog open={dialog === 'review'} onOpenChange={(o) => setDialog(o ? 'review' : '')} devices={selectedDevices} hiddenIds={hiddenIds}
        onRemove={(id) => setSelected((cur) => { const n = new Map(cur); n.delete(id); return n })} onClear={() => setSelected(new Map())} />
      <AssignProfileDialog open={dialog === 'assign'} onOpenChange={(o) => setDialog(o ? 'assign' : '')} credentials={creds.data?.data || []}
        platforms={(platforms.data?.data || []).filter((p: any) => p.value !== 'autodetect')} devices={selectedDevices} hiddenIds={hiddenIds} onDone={afterBulk} />
      <BulkSettingsDialog open={dialog === 'settings'} onOpenChange={(o) => setDialog(o ? 'settings' : '')} devices={selectedDevices} hiddenIds={hiddenIds} onDone={afterBulk} />
      <BulkBackupDialog open={dialog === 'backup'} onOpenChange={(o) => setDialog(o ? 'backup' : '')} devices={selectedDevices.filter((d) => d.enrolled)} hiddenIds={hiddenIds} onDone={afterBulk}
        onPrune={(gone) => setSelected((cur) => { const n = new Map(cur); gone.forEach((id) => n.delete(id)); return n })} />
    </div>
  )
}

function ReasonLine({ d }: { d: any }) {
  if (d.backup_state === 'failed' || d.backup_state === 'blocked') {
    return <span title={d.last_error || ''}>{d.last_failure_category ? categoryLabel(d.last_failure_category) : 'Failed'}{d.open_incident ? ` · ${d.open_incident.occurrences}× since ` : ''}{d.open_incident && <Ago iso={d.open_incident.opened_at} />}</span>
  }
  if (d.eligibility_review) return <>Review inventory type/address before deciding backup scope</>
  if (!d.eligible && !d.enrolled) return <>{d.eligibility_reason}</>
  if (d.support?.state === 'unsupported') return <>{d.support.message}</>
  if (d.backup_state === 'unconfigured') return <>In scope — enroll with a connection profile</>
  if (d.backup_state === 'pending') return <>Run the first backup</>
  if (d.backup_state === 'stale') return <>Older than {d.freshness_hours} h freshness limit</>
  if (d.backup_state === 'fresh') return <>{d.last_restore_test?.result === 'passed' ? 'Restore-tested ' : 'Not restore-tested'}{d.last_restore_test?.result === 'passed' && <Ago iso={d.last_restore_test.tested_at} />}</>
  return <>{STATES[d.backup_state]?.description || ''}</>
}


