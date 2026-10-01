import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowDownWideNarrow, ArrowUpWideNarrow, Clock3, Download, SlidersHorizontal, TrendingUp } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { TimeRangePicker } from '@/components/TimeRangePicker'
import { DataQualityBar } from '@/components/netflow/DataQualityBar'
import { FilterChips } from '@/components/netflow/FilterChips'
import { NetflowPageHeader } from '@/components/netflow/NetflowPageHeader'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { serverNow } from '@/lib/serverClock'
import { cn } from '@/lib/utils'
import { nfParams, useNfCoverage, useNfQuery } from '@/lib/netflow/api'
import { formatPercent } from '@/lib/netflow/format'
import { coverageState } from '@/lib/netflow/status'
import { useFlowFilter } from '@/lib/netflow/useFlowFilter'
import { useNfWindow } from '@/lib/netflow/window'
import { ExporterDeviceDialog } from './components/ExporterDeviceDialog'
import { BillingReport } from './capacity/BillingReport'
import { CapacityTable, Pager } from './capacity/CapacityTable'
import { CoverageCard, StateTiles } from './capacity/CoverageCard'
import { InterfaceDialog } from './capacity/InterfaceDialog'
import { SettingsDialog } from './capacity/SettingsDialog'
import { SpeedDialog } from './capacity/SpeedDialog'
import { downloadCsv, selectCls } from './capacity/shared'
import {
  businessHoursText,
  lastMonth,
  provisionalText,
  RESOLUTION_SHORT,
  RESOLUTIONS,
  resolutionAction,
  ROLE_LABELS,
  ROLES,
  SORT_OPTIONS,
  STATE_LABELS,
  STATES,
  type CapacityData,
  type CapacityRow,
} from './capacity/model'

const PAGE_SIZES = [25, 50, 100]

/**
 * NetFlow capacity (B4): per-direction interface utilization from complete 5-minute samples
 * (NF-13, NF-14), remaining capacity (NF-15), setup actions for every interface whose capacity
 * is unknown (NF-16), a configurable percentile billing report (CR-08) and per-interface trends
 * with a runout forecast (CR-09, FE-05). Every count covers the whole authorized set, not the
 * loaded page.
 */
export function NetflowCapacityPage() {
  const navigate = useNavigate()
  const [sp, setSp] = useSearchParams()
  const { window: w, rangeIdx, isCustom, range, setPreset, setCustom, setCalendar } = useNfWindow()
  const { filter, errors, invalid, removeKeys, clearAll } = useFlowFilter()

  // A capacity view over one hour says little: default to a week.
  useEffect(() => {
    if (!sp.get('range')) {
      const next = new URLSearchParams(sp)
      next.set('range', '7d')
      setSp(next, { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const tab = sp.get('tab') === 'billing' ? 'billing' : 'utilization'
  const q = sp.get('q') ?? ''
  const state = sp.get('state')
  const res = sp.get('res')
  const role = sp.get('role')
  const sort = SORT_OPTIONS.some((o) => o.value === sp.get('sort')) ? sp.get('sort')! : 'risk'
  const order = sp.get('order') === 'asc' ? 'asc' : 'desc'
  const size = PAGE_SIZES.includes(Number(sp.get('size'))) ? Number(sp.get('size')) : 50
  const offset = Math.max(0, Number(sp.get('offset')) || 0)
  const bh = sp.get('bh') === '1'
  const ignored = sp.get('ignored') === '1'

  const setParams = (patch: Record<string, string | null>, keepOffset = false) => {
    const next = new URLSearchParams(sp)
    for (const [k, v] of Object.entries(patch)) {
      if (v == null || v === '') next.delete(k)
      else next.set(k, v)
    }
    if (!keepOffset) next.delete('offset')
    setSp(next, { replace: true })
  }

  const [search, setSearch] = useState(q)
  useEffect(() => setSearch(q), [q])
  useEffect(() => {
    if (search === q) return
    const t = window.setTimeout(() => setParams({ q: search.trim() || null }), 350)
    return () => window.clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search])

  const extra = {
    limit: size,
    offset,
    q: q || undefined,
    state: state || undefined,
    resolution: res || undefined,
    role: role || undefined,
    sort,
    order,
    compare: 1,
    business_hours: bh ? 1 : undefined,
    include_ignored: ignored ? 1 : undefined,
  }
  const params = nfParams(w, filter, extra)
  const cap = useNfQuery<CapacityData>('/netflow/capacity', params, { window: w, enabled: tab === 'utilization' })
  const cov = useNfCoverage(w, filter, tab === 'utilization')
  const data = cap.data?.data
  const perms = data?.permissions ?? { manage: false, device_speed: false }

  const [detail, setDetail] = useState<CapacityRow | null>(null)
  const [speedRow, setSpeedRow] = useState<CapacityRow | null>(null)
  // B6's mapping dialog: the searchable exporter list (null) or one exporter; undefined = closed.
  const [linkTarget, setLinkTarget] = useState<string | null | undefined>(undefined)
  const [settingsOpen, setSettingsOpen] = useState(false)

  // Keep an open detail in step with refreshed data (same interface, new numbers).
  const detailRow = useMemo(() => (detail ? data?.items.find((r) => r.key === detail.key) ?? detail : null), [detail, data])

  const onResolve = (row: CapacityRow) => {
    const act = resolutionAction(row, perms)
    if (act.kind === 'link_exporter') setLinkTarget(row.exporter)
    else if (act.kind === 'open_device' && row.device_id) navigate(`/devices/${row.device_id}?tab=inventory`)
    else if (act.kind === 'open_interfaces' && row.device_id) navigate(`/devices/${row.device_id}/interfaces`)
    else setSpeedRow(row)
  }

  const statistic = data?.method.statistic ?? 'p95'
  const levels = { warn: data?.method.thresholds.warn_pct ?? 70, crit: data?.method.thresholds.crit_pct ?? 90 }
  const nowMonth = lastMonth(serverNow(), w.tz)
  const month = /^\d{4}-\d{2}$/.test(sp.get('month') ?? '') ? sp.get('month')! : nowMonth
  const settingsQuery = useNfQuery<{ settings: CapacityData['settings']; info: Record<string, unknown>; permissions: CapacityData['permissions'] }>(
    '/netflow/capacity/settings', null, { refetchInterval: false },
  )
  const settings = data?.settings ?? (settingsQuery.data?.data ? { ...settingsQuery.data.data.settings, ...settingsQuery.data.data.info } : null)
  const canManage = data?.permissions.manage ?? settingsQuery.data?.data.permissions.manage ?? false

  return (
    <div className="space-y-4">
      <NetflowPageHeader
        title="Capacity"
        icon={TrendingUp}
        description="Per-direction interface utilization from flow records over complete 5-minute samples: in = received, out = transmitted, each against its own speed."
        actions={
          <>
            {tab === 'utilization' && (
            <TimeRangePicker
              rangeIdx={rangeIdx}
              isCustom={isCustom}
              customFrom={range.fromISO}
              customTo={range.toISO}
              onPreset={setPreset}
              onCustom={setCustom}
              calendarPresets
              activeKey={range.key}
              onCalendar={setCalendar}
            />
            )}
            {tab === 'utilization' && (
              <Button
                size="sm"
                variant={bh ? 'default' : 'outline'}
                aria-pressed={bh}
                onClick={() => setParams({ bh: bh ? null : '1' })}
                title={settings ? `Only ${businessHoursText(settings.business_hours)}` : 'Only the configured business hours'}
              >
                <Clock3 aria-hidden />
                Business hours
              </Button>
            )}
            <Button size="sm" variant="outline" onClick={() => setSettingsOpen(true)} disabled={!settings}>
              <SlidersHorizontal aria-hidden />
              Methodology
            </Button>
            {tab === 'utilization' && (
              <Button size="sm" variant="outline" disabled={!data}
                onClick={() => downloadCsv('/netflow/capacity', { ...params, limit: undefined, offset: undefined, compare: undefined }, `netflow-capacity-${w.key}.csv`)}>
                <Download aria-hidden />
                Export CSV
              </Button>
            )}
          </>
        }
      />

      <FilterChips
        filter={filter}
        invalid={invalid}
        errors={errors}
        tz={w.tz}
        onRemove={(keys) => removeKeys(keys)}
        onClearAll={clearAll}
      />

      <Tabs value={tab} onValueChange={(v) => setParams({ tab: v === 'billing' ? 'billing' : null }, true)}>
        <TabsList>
          <TabsTrigger value="utilization">Utilization</TabsTrigger>
          <TabsTrigger value="billing">Billing report</TabsTrigger>
        </TabsList>

        <TabsContent value="utilization" className="space-y-4">
          <DataQualityBar meta={cap.data?.meta} window={w} filter={filter} coverage={cov.data?.data ?? undefined} />
          <QueryPanel
            query={cap}
            label="capacity"
            skeleton="tiles"
            coverage={coverageState(cov.data?.data)}
            isEmpty={(r) => r.data.summary.interfaces_total === 0}
            empty="No interface carried flow traffic in this period"
            noCoverage="The exporters in scope delivered no flow records in this period, so interface utilization is unknown here, not zero."
          >
            {(r) => {
              const d = r.data
              return (
                <div className="space-y-4">
                  <CoverageCard
                    summary={d.summary}
                    method={d.method}
                    permissions={d.permissions}
                    activeResolution={res}
                    onResolution={(v) => setParams({ res: v })}
                    onLinkExporters={() => setLinkTarget(null)}
                  />
                  <StateTiles
                    summary={d.summary}
                    method={d.method}
                    active={state}
                    onToggle={(s) => setParams({ state: state === s ? null : s })}
                  />
                  <section aria-labelledby="cap-table" className="space-y-3 rounded-lg border border-border bg-surface p-4 shadow-card">
                    <div className="flex flex-col gap-1">
                      <h2 id="cap-table" className="text-sm font-semibold text-text">Interfaces by utilization risk</h2>
                      <p className="text-[11px] text-text2">
                        {d.method.statistic} per direction by nearest rank over {d.window.expected_slots.toLocaleString()} expected 5-minute samples
                        {d.window.business_hours ? ' in business hours' : ''}; warning at {formatPercent(levels.warn)}, critical at {formatPercent(levels.crit)}.
                        Not a billing figure: use the billing report for a complete period.
                        {provisionalText(d.window.provisional) && ` ${provisionalText(d.window.provisional)}`}
                      </p>
                    </div>
                    <div className="grid grid-cols-2 gap-2 lg:grid-cols-4 xl:flex xl:flex-wrap xl:items-end">
                      <label className="col-span-2 space-y-1 text-xs font-medium text-text lg:col-span-4 xl:w-64">
                        Search
                        <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Interface, device, exporter, ifIndex" />
                      </label>
                      <label className="space-y-1 text-xs font-medium text-text">
                        State
                        <select className={cn(selectCls, 'block w-full xl:w-40')} value={state ?? ''} onChange={(e) => setParams({ state: e.target.value || null })}>
                          <option value="">All states</option>
                          {STATES.map((s) => <option key={s} value={s}>{STATE_LABELS[s]}</option>)}
                        </select>
                      </label>
                      <label className="space-y-1 text-xs font-medium text-text">
                        Capacity
                        <select className={cn(selectCls, 'block w-full xl:w-44')} value={res ?? ''} onChange={(e) => setParams({ res: e.target.value || null })}>
                          <option value="">Known and unknown</option>
                          <option value="unresolved">Unknown (any reason)</option>
                          {RESOLUTIONS.map((x) => <option key={x} value={x}>{RESOLUTION_SHORT[x]}</option>)}
                        </select>
                      </label>
                      <label className="space-y-1 text-xs font-medium text-text">
                        Role
                        <select className={cn(selectCls, 'block w-full xl:w-32')} value={role ?? ''} onChange={(e) => setParams({ role: e.target.value || null })}>
                          <option value="">All roles</option>
                          {ROLES.map((x) => <option key={x} value={x}>{ROLE_LABELS[x]}</option>)}
                          <option value="none">No role</option>
                        </select>
                      </label>
                      <div className="flex items-end gap-1">
                        <label className="flex-1 space-y-1 text-xs font-medium text-text">
                          Sort by
                          <select className={cn(selectCls, 'block w-full xl:w-48')} value={sort} onChange={(e) => setParams({ sort: e.target.value === 'risk' ? null : e.target.value })}>
                            {SORT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                          </select>
                        </label>
                        <Button
                          variant="outline"
                          size="icon"
                          aria-label={order === 'desc' ? 'Descending order; switch to ascending' : 'Ascending order; switch to descending'}
                          title={order === 'desc' ? 'Descending' : 'Ascending'}
                          onClick={() => setParams({ order: order === 'desc' ? 'asc' : null })}
                        >
                          {order === 'desc' ? <ArrowDownWideNarrow aria-hidden /> : <ArrowUpWideNarrow aria-hidden />}
                        </Button>
                      </div>
                      <label className="space-y-1 text-xs font-medium text-text">
                        Per page
                        <select className={cn(selectCls, 'block w-full xl:w-24')} value={size} onChange={(e) => setParams({ size: e.target.value === '50' ? null : e.target.value })}>
                          {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
                        </select>
                      </label>
                      <label className="col-span-2 inline-flex h-9 items-center gap-2 text-xs font-medium text-text lg:col-span-1 xl:col-span-1">
                        <input type="checkbox" checked={ignored} onChange={(e) => setParams({ ignored: e.target.checked ? '1' : null })} />
                        Show ignored interfaces
                      </label>
                    </div>
                    {d.items.length === 0 ? (
                      <p role="status" className="rounded-md border border-dashed border-border p-6 text-center text-sm text-text2">
                        No interface matches the search and filters.
                      </p>
                    ) : (
                      <CapacityTable
                        rows={d.items}
                        statistic={d.method.statistic}
                        perms={d.permissions}
                        compare
                        actions={{ onDetail: setDetail, onResolve }}
                      />
                    )}
                    <Pager offset={d.offset} limit={d.limit} matched={d.matched} onPage={(o) => setParams({ offset: o ? String(o) : null }, true)} />
                  </section>
                </div>
              )
            }}
          </QueryPanel>
        </TabsContent>

        <TabsContent value="billing">
          <BillingReport
            filter={filter}
            tz={w.tz}
            settings={settings}
            month={month}
            onMonth={(m) => setParams({ month: m === nowMonth ? null : m }, true)}
            percentile={sp.get('bp') ? Number(sp.get('bp')) : null}
            onPercentile={(p) => setParams({ bp: p == null ? null : String(p) }, true)}
            mode={sp.get('bm')}
            onMode={(m) => setParams({ bm: m }, true)}
            businessHours={sp.get('bbh') == null ? null : sp.get('bbh') === '1'}
            onBusinessHours={(b) => setParams({ bbh: b == null ? null : b ? '1' : '0' }, true)}
            defaultMonth={nowMonth}
          />
        </TabsContent>
      </Tabs>

      <InterfaceDialog
        row={detailRow}
        open={!!detail}
        onOpenChange={(v) => !v && setDetail(null)}
        window={w}
        filter={filter}
        statistic={statistic}
        levels={levels}
        perms={perms}
        compare
        onSetSpeed={(row) => setSpeedRow(row)}
        onLinkExporter={(row) => setLinkTarget(row.exporter)}
      />
      <SpeedDialog row={speedRow} perms={perms} open={!!speedRow} onOpenChange={(v) => !v && setSpeedRow(null)} />
      {settings && (
        <SettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} settings={settings} method={data?.method ?? null} canManage={canManage} />
      )}
      <ExporterDeviceDialog
        open={linkTarget !== undefined}
        onOpenChange={(v) => !v && setLinkTarget(undefined)}
        exporter={linkTarget ?? null}
      />
    </div>
  )
}
