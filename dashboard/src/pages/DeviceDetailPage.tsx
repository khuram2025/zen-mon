import { useCallback, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ArrowLeft, Loader2 } from 'lucide-react'
import { alignDeviceRange } from '@/components/devices/templatePresentation'
import { TemplateInsightsSection, type ChartRequest, type InsightGroup } from '@/components/devices/TemplateInsightsSection'
import { DeviceFormDialog } from '@/components/forms/DeviceFormDialog'
import { useBreadcrumbDetail } from '@/components/layout/breadcrumbDetail'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { toast } from '@/components/ui/Toast'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { ConfigurationTab } from './device/ConfigurationTab'
import { DeviceHeader } from './device/DeviceHeader'
import { DeviceMaintenanceDialog } from './device/DeviceActions'
import { EventsTab } from './device/EventsTab'
import { displayTimezone, formatRelative } from './device/format'
import { HardwareTab } from './device/HardwareTab'
import { useDeviceRecord, useDeviceSummary, useSnmpMetrics } from './device/hooks'
import { InterfacesTab } from './device/InterfacesTab'
import { IssuesBar } from './device/IssuesBar'
import { PerformanceTab, type PinnedMetric } from './device/PerformanceTab'
import { SummaryTab } from './device/SummaryTab'
import { deviceTabs, isHardwareGroup, issueTab, resolveTab, type DeviceTabId } from './device/tabs'
import type { DeviceIssue } from './device/types'

/* The device page answers three questions in order: is this device healthy,
 * what is wrong right now, and where do I go next.
 *
 *   1. Header (sticky): identity, the confirmed status beside a separate
 *      health rollup, one action bar.
 *   2. Issues bar: open problems, worst first.
 *   3. Tabs with the range picker (hidden where time does not apply).
 *   4. Tab content; a tab fetches its own data only while it is open. */

const vendorGroups = (g: InsightGroup) => !isHardwareGroup(g)

export function DeviceDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const { range: requestedRange, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const [applianceTime, setApplianceTime] = useState<number | null>(null)
  const range = alignDeviceRange(requestedRange, applianceTime)
  const [editOpen, setEditOpen] = useState(false)
  const [delOpen, setDelOpen] = useState(false)
  const [maintOpen, setMaintOpen] = useState(false)
  const issuesRef = useRef<HTMLElement>(null)

  const deviceQuery = useDeviceRecord(id, setApplianceTime)
  const device = deviceQuery.data
  const summaryQuery = useDeviceSummary(device ? id : undefined)
  const summary = summaryQuery.data
  useBreadcrumbDetail(device?.hostname)
  // Uptime for the header: sysUpTime from the shortest SNMP window (shared with the Summary tab's 1h query).
  const uptimeMetrics = useSnmpMetrics(id || '', 1, !!device?.snmp_enabled)
  const uptimePoints = (uptimeMetrics.data?.uptime || uptimeMetrics.data?.sysUpTime || uptimeMetrics.data?.sys_uptime)?.points
  const uptimeSeconds = typeof device?.uptime_seconds === 'number' ? device.uptime_seconds
    : uptimePoints?.length ? uptimePoints[uptimePoints.length - 1].value : null

  const { data: maint } = useQuery<{ active: any[]; upcoming: any[] }>({
    queryKey: ['device-maintenance', id],
    queryFn: async () => (await api.get(`/devices/${id}/maintenance`)).data,
    enabled: !!id && maintOpen,
  })

  const del = useMutation({
    mutationFn: async () => api.delete(`/devices/${id}`),
    onSuccess: () => {
      toast.success('Device deleted')
      qc.invalidateQueries({ queryKey: ['devices'] })
      navigate('/devices')
    },
    onError: (e: unknown) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const tabs = useMemo(() => (device ? deviceTabs(device, summary?.template?.vendor) : []), [device, summary?.template?.vendor])
  const tab = resolveTab(searchParams.get('tab'), tabs)
  const group = searchParams.get('group') || undefined
  const pinned: PinnedMetric | null = searchParams.get('pin') ? {
    seriesKey: searchParams.get('pin')!,
    title: searchParams.get('pin_title') || searchParams.get('pin')!,
    unit: searchParams.get('pin_unit'),
    warn: searchParams.get('pin_warn') ? Number(searchParams.get('pin_warn')) : null,
    crit: searchParams.get('pin_crit') ? Number(searchParams.get('pin_crit')) : null,
  } : null

  const openTab = useCallback((next: DeviceTabId, opts: { group?: string; pin?: ChartRequest } = {}) => {
    setSearchParams((previous) => {
      const p = new URLSearchParams(previous)
      let target = next
      if (opts.group) {
        const kind = summary?.health.groups.find((g) => g.key === opts.group)?.kind
        target = isHardwareGroup({ key: opts.group, kind }) ? 'hardware' : 'metrics'
      }
      if (!tabs.some((t) => t.id === target)) target = 'summary'
      p.set('tab', target)
      if (opts.group) p.set('group', opts.group)
      else p.delete('group')
      for (const k of ['pin', 'pin_title', 'pin_unit', 'pin_warn', 'pin_crit']) p.delete(k)
      if (opts.pin) {
        p.set('pin', opts.pin.seriesKey)
        p.set('pin_title', opts.pin.title)
        if (opts.pin.unit) p.set('pin_unit', opts.pin.unit)
        if (opts.pin.warn != null) p.set('pin_warn', String(opts.pin.warn))
        if (opts.pin.crit != null) p.set('pin_crit', String(opts.pin.crit))
      }
      return p
    })
    if (opts.pin || opts.group) window.requestAnimationFrame(() => document.getElementById('device-tabs')?.scrollIntoView({ block: 'start' }))
  }, [setSearchParams, summary?.health.groups, tabs])

  const openIssue = useCallback((issue: DeviceIssue) => {
    if (issue.source === 'alert' && issue.alert_id) { navigate(`/alerts/${issue.alert_id}`); return }
    const kindOf = (key: string) => summary?.health.groups.find((g) => g.key === key)?.kind
    openTab(issueTab(issue, tabs, kindOf), { group: issue.group })
  }, [navigate, openTab, summary?.health.groups, tabs])

  const chart = useCallback((request: ChartRequest) => openTab('performance', { pin: request }), [openTab])

  if (deviceQuery.isError && !device) {
    const status = (deviceQuery.error as { response?: { status?: number } })?.response?.status
    return (
      <div className="mx-auto max-w-xl space-y-3 py-16 text-center">
        <AlertTriangle className="mx-auto h-8 w-8 text-warning" aria-hidden />
        <h1 className="text-xl font-semibold">{status === 404 ? 'Device not found' : 'This device could not be loaded'}</h1>
        <p className="text-sm text-muted">{status === 404 ? 'It may have been deleted, or it is outside the device groups you can see.' : apiErrorMessage(deviceQuery.error)}</p>
        {status !== 404 && <QueryFailure error={deviceQuery.error} onRetry={() => deviceQuery.refetch()} retrying={deviceQuery.isFetching} what="the device" />}
        <Button variant="outline" asChild><Link to="/devices"><ArrowLeft className="h-4 w-4" />Back to devices</Link></Button>
      </div>
    )
  }
  if (!device) {
    return (
      <div className="mx-auto max-w-[1760px] space-y-3" aria-busy="true" aria-label="Loading device">
        <div className="rounded-xl border border-border bg-surface p-4"><Skeleton className="h-6 w-64" /><Skeleton className="mt-3 h-4 w-96 max-w-full" /><Skeleton className="mt-4 h-4 w-full" /></div>
        <Skeleton className="h-10 w-full" />
        <div className="grid grid-cols-2 gap-3 md:grid-cols-6">{Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-28" />)}</div>
        <div className="flex items-center justify-center gap-2 py-6 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" />Loading device…</div>
      </div>
    )
  }

  const activeTab = tabs.find((t) => t.id === tab)
  return (
    <div className="mx-auto max-w-[1760px] space-y-3">
      <DeviceHeader device={device} summary={summary} uptimeSeconds={uptimeSeconds}
        onShowIssues={() => issuesRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })}
        onEdit={() => setEditOpen(true)} onDelete={() => setDelOpen(true)} onMaintenance={() => setMaintOpen(true)} />
      <IssuesBar ref={issuesRef} summary={summary} loading={summaryQuery.isPending} onOpen={openIssue} />

      <Tabs value={tab} onValueChange={(v) => openTab(v as DeviceTabId)}>
        <div id="device-tabs" className="flex scroll-mt-28 flex-wrap items-center justify-between gap-2 border-b border-border pb-2">
          <TabsList aria-label="Device sections" className="h-auto flex-wrap">
            {tabs.map((t) => (
              <TabsTrigger key={t.id} value={t.id}>
                {t.label}
                {t.id === 'interfaces' && summary?.interfaces ? <span className="ml-1.5 rounded-full bg-surface2 px-1.5 text-xs text-muted">{summary.interfaces.total}</span> : null}
                {t.id === 'events' && summary?.alerts?.active ? <span className="ml-1.5 rounded-full bg-danger/15 px-1.5 text-xs text-danger">{summary.alerts.active}</span> : null}
              </TabsTrigger>
            ))}
          </TabsList>
          <div className="flex flex-wrap items-center gap-3">
            {activeTab?.usesRange && <>
              <span className="text-xs text-muted">Times in {displayTimezone()}</span>
              <TimeRangePicker rangeIdx={rangeIdx} isCustom={isCustom} customFrom={range.fromISO} customTo={range.toISO} onPreset={setPreset} onCustom={setCustom} />
            </>}
            {summaryQuery.dataUpdatedAt > 0 && (
              <span className="text-xs text-muted" aria-live="polite">
                {summaryQuery.isFetching ? 'Updating…' : `Updated ${formatRelative(summaryQuery.dataUpdatedAt)}`}
              </span>
            )}
          </div>
        </div>

        <TabsContent value="summary"><SummaryTab device={device} summary={summary} range={range} onOpen={(t, g) => openTab(t, { group: g })} /></TabsContent>
        <TabsContent value="performance"><PerformanceTab device={device} summary={summary} range={range} pinned={pinned} onClearPin={() => openTab('performance')} /></TabsContent>
        {tabs.some((t) => t.id === 'interfaces') && <TabsContent value="interfaces"><InterfacesTab device={device} range={range} /></TabsContent>}
        {tabs.some((t) => t.id === 'metrics') && (
          <TabsContent value="metrics">
            <TemplateInsightsSection key={`metrics-${group || 'all'}`} deviceId={device.id} vendor={device.vendor || undefined} deviceType={device.device_type}
              rangeHours={range.hours} initialGroup={tab === 'metrics' ? group : undefined} groupFilter={vendorGroups}
              title={activeTab?.label || 'Device metrics'} onChart={chart} showPromote />
          </TabsContent>
        )}
        {tabs.some((t) => t.id === 'hardware') && <TabsContent value="hardware"><HardwareTab key={`hardware-${group || 'all'}`} device={device} range={range} onChart={chart} initialGroup={tab === 'hardware' ? group : undefined} /></TabsContent>}
        <TabsContent value="events"><EventsTab device={device} summary={summary} range={range} /></TabsContent>
        <TabsContent value="configuration"><ConfigurationTab device={device} summary={summary} onOpenHardware={() => openTab('hardware')} /></TabsContent>
      </Tabs>

      <DeviceFormDialog open={editOpen} onOpenChange={setEditOpen} device={device} />
      <DeviceMaintenanceDialog open={maintOpen} onOpenChange={setMaintOpen} device={device} windows={maint} />
      <ConfirmDialog
        open={delOpen}
        onOpenChange={setDelOpen}
        title="Delete device"
        description={<>Permanently delete <b>{device.hostname}</b> and all its history?</>}
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => del.mutate()}
      />
    </div>
  )
}

export { InventoryConfigCard } from './device/ConfigurationTab'
