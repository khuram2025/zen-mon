import { useId, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { Cable, HeartPulse, PlugZap, RefreshCw, Search } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { ClockSkewBanner } from '@/components/services/list/ClockSkewBanner'
import { NetflowPageHeader } from '@/components/netflow/NetflowPageHeader'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { StatusTile } from '@/components/netflow/StatusTile'
import { DataQualityBar } from '@/components/netflow/DataQualityBar'
import { SCOPE_EMPTY_TITLE } from '@/components/netflow/NetflowAccess'
import { nfKeys, useNfQuery } from '@/lib/netflow/api'
import { formatCount, formatCountExact, formatDateTime } from '@/lib/netflow/format'
import { displayTimeZone } from '@/lib/netflow/window'
import { cn } from '@/lib/utils'
import { ExporterDeviceDialog } from './components/ExporterDeviceDialog'
import { CollectorPanel, COLLECTOR_WINDOWS, useCollectorHealth, type CollectorWindow } from './health/CollectorPanel'
import { CountingPanel } from './health/CountingPanel'
import { ExporterDetailDialog, type DetailTab } from './health/ExporterDetailDialog'
import { ExporterTable } from './health/ExporterTable'
import { RetentionPanel } from './health/RetentionPanel'
import {
  EXPORTER_FILTERS,
  collectorCaption,
  collectorTone,
  collectorWord,
  droppedRecords,
  filterExporters,
  formatAge,
  receivingTone,
  sortExporters,
  summarizeExporters,
  type ExporterFilterKey,
  type HealthStatus,
} from './health/model'
import { Segmented } from './health/ui'

type PageTab = 'exporters' | 'collector' | 'counting' | 'retention'
const TABS: PageTab[] = ['exporters', 'collector', 'counting', 'retention']
const DETAIL_TABS: DetailTab[] = ['overview', 'fields', 'interfaces', 'settings', 'device']

/**
 * Collector & exporter health (NF-01, FE-01 / CR-05, FE-11, FE-12). The place for exporter
 * health: receipt state computed by the appliance independent of any dashboard window or filter,
 * the collector process and its statistics, how traffic is counted across collection points, and
 * how long each dataset is kept. Tab, filters and the open exporter live in the URL.
 */
export function NetflowHealthPage() {
  const [sp, setSp] = useSearchParams()
  const tab = (TABS.includes(sp.get('tab') as PageTab) ? sp.get('tab') : 'exporters') as PageTab
  const stateFilter = (EXPORTER_FILTERS.some((f) => f.key === sp.get('state')) ? sp.get('state') : 'all') as ExporterFilterKey
  const q = sp.get('q') ?? ''
  const hours = (COLLECTOR_WINDOWS.some((w) => w.value === sp.get('hours')) ? sp.get('hours') : '1') as CollectorWindow
  const openExporter = sp.get('exporter')
  const detailTab = (DETAIL_TABS.includes(sp.get('detail') as DetailTab) ? sp.get('detail') : 'overview') as DetailTab

  const patch = (changes: Record<string, string | null>, replace = true) => {
    const next = new URLSearchParams(sp)
    for (const [k, v] of Object.entries(changes)) {
      if (v === null || v === '' || (k === 'tab' && v === 'exporters') || (k === 'state' && v === 'all') || (k === 'hours' && v === '1') || (k === 'detail' && v === 'overview')) next.delete(k)
      else next.set(k, v)
    }
    setSp(next, { replace })
  }
  const openDetail = (ip: string, t: DetailTab = 'overview') => patch({ exporter: ip, detail: t }, false)

  const qc = useQueryClient()
  const [linksOpen, setLinksOpen] = useState(false)
  const status = useNfQuery<HealthStatus>('/netflow/exporters/status', null, {
    queryKey: nfKeys.exporterStatus(),
    refetchInterval: 30_000,
    keepPrevious: false,
  })
  const collector = useCollectorHealth(hours)
  const rows = status.data?.data.exporters ?? []
  const summary = useMemo(() => summarizeExporters(rows), [rows])
  const visible = useMemo(() => sortExporters(filterExporters(rows, stateFilter, q)), [rows, stateFilter, q])
  const counts = useMemo(() => {
    const out: Record<string, number> = {}
    for (const f of EXPORTER_FILTERS) out[f.key] = filterExporters(rows, f.key).length
    return out
  }, [rows])
  const searchId = useId()
  const tz = displayTimeZone()
  const live = collector.data?.data.live
  const totals = collector.data?.data.stats?.totals

  return (
    <div className="space-y-4">
      <NetflowPageHeader
        title="Collector health"
        icon={HeartPulse}
        description="Whether each exporter's flows reach the collector and can be used, measured on the appliance clock and independent of dashboard filters and time ranges."
        actions={
          <>
            <button
              type="button"
              onClick={() => qc.invalidateQueries({ queryKey: nfKeys.all })}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1.5 text-xs font-medium text-text hover:bg-surface2"
              aria-label="Refresh collector health"
            >
              <RefreshCw className={cn('h-3.5 w-3.5', (status.isFetching || collector.isFetching) && 'animate-spin')} aria-hidden />
              Refresh
            </button>
            <button
              type="button"
              onClick={() => setLinksOpen(true)}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1.5 text-xs font-medium text-text hover:bg-surface2"
            >
              <Cable className="h-3.5 w-3.5" aria-hidden />
              Device links
            </button>
            <Link
              to="/netflow/setup"
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1.5 text-xs font-medium text-text hover:bg-surface2"
            >
              <PlugZap className="h-3.5 w-3.5" aria-hidden />
              Add an exporter
            </Link>
          </>
        }
      />
      <ClockSkewBanner />

      <section aria-label="Summary" className="grid grid-cols-1 gap-3 min-[420px]:grid-cols-2 lg:grid-cols-4">
        <StatusTile
          tone={status.status === 'success' ? receivingTone(summary) : 'unknown'}
          loading={status.status === 'pending'}
          label="Exporters receiving"
          value={status.status === 'success' ? `${summary.receiving} of ${summary.total}` : null}
          word={status.status !== 'success' ? 'Unknown' : summary.silent ? 'Silent' : summary.late ? 'Late' : summary.total ? 'Receiving' : 'None yet'}
          caption={status.status === 'success'
            ? [summary.late && `${summary.late} late`, summary.silent && `${summary.silent} silent`, summary.never && `${summary.never} never received`]
                .filter(Boolean).join(' · ') || 'All exporters delivering on time'
            : status.status === 'error' ? 'Status could not be loaded' : undefined}
          onClick={() => patch({ tab: null, state: summary.silent || summary.late ? 'problems' : 'all' })}
          title="Show exporters"
        />
        <StatusTile
          tone={status.status === 'success' ? (summary.withProblems ? 'warning' : 'success') : 'unknown'}
          loading={status.status === 'pending'}
          label="Exporters needing attention"
          value={status.status === 'success' ? String(summary.withProblems) : null}
          word={status.status !== 'success' ? 'Unknown' : summary.withProblems ? 'Check' : 'None'}
          caption={status.status === 'success' ? `${summary.unmapped} without a linked device` : undefined}
          onClick={() => patch({ tab: null, state: 'problems' })}
          active={tab === 'exporters' && stateFilter === 'problems'}
          title="Show exporters that need attention"
        />
        <StatusTile
          tone={collector.status === 'success' && live ? collectorTone(live.status, live.reachable) : 'unknown'}
          loading={collector.status === 'pending'}
          label="Collector"
          value={live ? (live.reachable ? (live.listening ? `Up ${formatAge(live.uptime_s)}` : 'Not listening') : 'No answer') : null}
          word={live ? collectorWord(live.status, live.reachable) : 'Unknown'}
          caption={collectorCaption(collector.status, live)}
          onClick={() => patch({ tab: 'collector' })}
          title="Show the collector"
        />
        <StatusTile
          tone={totals ? (droppedRecords(totals) > 0 ? 'warning' : 'neutral') : 'unknown'}
          loading={collector.status === 'pending'}
          label={`Records stored (${COLLECTOR_WINDOWS.find((w) => w.value === hours)?.label})`}
          value={totals ? formatCount(totals.records_written) : null}
          word={totals ? (droppedRecords(totals) > 0 ? 'Drops' : null) : 'Unknown'}
          caption={totals ? `${formatCount(droppedRecords(totals))} dropped · ${formatCount(totals.data_sets_waiting_template)} data sets waited for a template` : undefined}
          onClick={() => patch({ tab: 'collector' })}
          title={totals ? `${formatCountExact(totals.records_written)} records stored — show the collector statistics` : 'Show the collector statistics'}
        />
      </section>

      <Tabs value={tab} onValueChange={(v) => patch({ tab: v })}>
        <div className="-mx-1 overflow-x-auto px-1">
          <TabsList aria-label="Collector health sections">
            <TabsTrigger value="exporters">Exporters</TabsTrigger>
            <TabsTrigger value="collector">Collector</TabsTrigger>
            <TabsTrigger value="counting">Counting</TabsTrigger>
            <TabsTrigger value="retention">Retention</TabsTrigger>
          </TabsList>
        </div>

        <TabsContent value="exporters">
          <Card>
            <CardHeader className="space-y-3 pb-3">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <CardTitle className="text-sm">Exporters</CardTitle>
                {status.data && (
                  <span className="text-[11px] text-text2">
                    States as of {formatDateTime(status.data.data.server_time, tz, { timeOnly: true })} (appliance) · refreshed every 30 s
                  </span>
                )}
              </div>
              <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
                <Segmented
                  label="Show exporters"
                  value={stateFilter}
                  onChange={(v) => patch({ state: v })}
                  options={EXPORTER_FILTERS.map((f) => ({ value: f.key, label: f.label, count: status.data ? counts[f.key] : undefined }))}
                />
                <div className="relative lg:w-72">
                  <label htmlFor={searchId} className="sr-only">Search exporters</label>
                  <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-text2" aria-hidden />
                  <input
                    id={searchId}
                    value={q}
                    onChange={(e) => patch({ q: e.target.value })}
                    placeholder="Address, name or device"
                    className="h-8 w-full rounded-md border border-border bg-surface pl-8 pr-3 text-xs focus:outline-none focus:ring-2 focus:ring-primary/40"
                  />
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <QueryPanel
                query={status}
                label="exporter status"
                skeleton="rows"
                isEmpty={(r) => r.data.exporters.length === 0 || visible.length === 0}
                empty={rows.length === 0
                  ? (status.data?.meta?.warnings ?? []).some((w) => w.code === 'scope_empty')
                    ? <span>{SCOPE_EMPTY_TITLE}: none of the devices your tag scope covers owns an exporter that has sent flows here.</span>
                    : <span>No exporter has sent flows to this appliance yet. <Link to="/netflow/setup" className="underline underline-offset-2">Set one up</Link>.</span>
                  : 'No exporter matches this filter'}
              >
                {(r) => <ExporterTable rows={visible} serverTime={r.data.server_time} onOpen={openDetail} />}
              </QueryPanel>
              {status.data && <DataQualityBar meta={status.data.meta} coverage={null} />}
              <p className="text-[11px] text-text2">
                Receiving: flows arrived within the expected interval (configured, or learned from the exporter&apos;s own cadence).
                Late: longer than two intervals. Silent: longer than ten intervals (at least 15 minutes). Silence says nothing
                about whether the device itself is reachable — its polled status is shown next to it.
              </p>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="collector">
          <CollectorPanel hours={hours} onHoursChange={(h) => patch({ hours: h })} query={collector} />
        </TabsContent>

        <TabsContent value="counting">
          <CountingPanel onEditExporter={(ip) => openDetail(ip, 'settings')} />
        </TabsContent>

        <TabsContent value="retention">
          <RetentionPanel />
        </TabsContent>
      </Tabs>

      <ExporterDeviceDialog open={linksOpen} onOpenChange={setLinksOpen} />
      {openExporter && (
        <ExporterDetailDialog
          exporter={openExporter}
          tab={detailTab}
          onTabChange={(t) => patch({ detail: t })}
          onClose={() => patch({ exporter: null, detail: null })}
        />
      )}
    </div>
  )
}
