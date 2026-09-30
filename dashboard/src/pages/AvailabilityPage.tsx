/**
 * Availability overview — a visual executive dashboard per domain
 * (components/availability/dashboards.tsx) with an Operations view for
 * technical depth and a NOC wall mode.
 *
 * Rules (see Availability management review A01–A15):
 *  - Missing evidence is "Not measured", never 0% and never a target verdict.
 *  - Loading, request failure, stale (previous period) and measured data have
 *    distinct presentations.
 *  - Current state ("now") and period performance are always separate.
 *  - Every headline, chip, card and the brief come from one assessment model
 *    (components/availability/model.ts).
 *  - Availability uses two truncated decimals; changes are percentage points.
 */

import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity, Download, ExternalLink, FileText, Loader2, Maximize2, Minimize2, Printer, Server, Shield,
  TriangleAlert,
} from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import {
  useAvailabilityPolicy, useBusinessReport, useExecutiveReport, useTechnicalReport,
} from '@/hooks/useReports'
import { useSSE, type SSEConnectionState } from '@/hooks/useSSE'
import type { ServerMonitoringOverview } from '@/types/servers'
import {
  PRESET_BUTTON_LABELS, PRESET_LABELS, fmtAge, fmtClock, fmtDateTime, fmtPeriod, fmtSkew, parseTs, timezoneLabel,
} from '@/components/availability/format'
import {
  assessDevices, assessServers, assessServices, complianceCounts, normalizeServices,
  type DeviceLite, type DeviceSummary, type FleetDomain, type ReportState, type ServiceSummary,
} from '@/components/availability/model'
import {
  DecisionMetrics, DevicesOperations, ServersOperations, ServicesOperations, type ViewCommon,
} from '@/components/availability/views'
import { DevicesDashboard, ServersDashboard, ServicesDashboard } from '@/components/availability/dashboards'
import { BriefPanel, DataErrorsBanner, ErrorPanel, TONE, ToneIcon } from '@/components/availability/ui'
import { PolicyControl } from '@/components/availability/PolicyControl'
import { buildAvailabilityCsv, downloadText } from '@/components/availability/exportCsv'

type ViewMode = 'executive' | 'operations'

/** Clock disagreement worth telling the reader about (beyond request
 *  latency and report generation time). */
const SKEW_WARN_MS = 5 * 60_000

const DOMAINS: Array<{ key: FleetDomain; label: string; icon: typeof Server }> = [
  { key: 'devices', label: 'Devices', icon: Server },
  { key: 'services', label: 'Services', icon: Shield },
  { key: 'servers', label: 'Servers', icon: Activity },
]

function reportState(q: { isLoading: boolean; isError: boolean; isPlaceholderData: boolean; data: unknown }): ReportState {
  return {
    loading: q.isLoading,
    error: q.isError,
    placeholder: q.isPlaceholderData,
    refreshFailed: q.isError && q.data != null,
  }
}

export function AvailabilityPage() {
  const qc = useQueryClient()
  const { range, rangeIdx, isCustom, setPreset, setCustom, setCalendar } = useTimeRange()
  const [params, setParams] = useSearchParams()
  const [noc, setNoc] = useState(false)

  const domain: FleetDomain = params.get('view') === 'services' || params.get('view') === 'servers'
    ? params.get('view') as FleetDomain
    : 'devices'
  const mode: ViewMode = params.get('mode') === 'operations' ? 'operations' : 'executive'
  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params)
    if (value == null) next.delete(key)
    else next.set(key, value)
    setParams(next, { replace: true })
  }

  /* Ages ("3m ago") refresh on a slow tick; no per-second page re-render. */
  const [nowTick, setNowTick] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNowTick(Date.now()), 15_000)
    return () => window.clearInterval(id)
  }, [])

  // A status change only moves the live counters; the period reports keep
  // their own refresh cadence.
  const stream = useSSE('/api/v1/stream/status', {
    enabled: true,
    onMessage: (event) => {
      if (event !== 'status_change') return
      for (const k of [['avail', 'summary'], ['avail', 'attention'], ['avail', 'services'], ['avail', 'servers']]) {
        qc.invalidateQueries({ queryKey: k })
      }
    },
  })

  useEffect(() => {
    if (!noc) return
    const aside = document.querySelector('aside') as HTMLElement | null
    const header = document.querySelector('header') as HTMLElement | null
    const prevAside = aside?.style.display ?? ''
    const prevHeader = header?.style.display ?? ''
    if (aside) aside.style.display = 'none'
    if (header) header.style.display = 'none'
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setNoc(false) }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      if (aside) aside.style.display = prevAside
      if (header) header.style.display = prevHeader
    }
  }, [noc])

  /* ── Period reports (only the active domain's) ─────────────────────── */
  const rangeArgs = { fromISO: range.fromISO, toISO: range.toISO }
  const execQ = useExecutiveReport(rangeArgs, { enabled: domain === 'devices' })
  const techQ = useTechnicalReport(rangeArgs, { enabled: domain === 'devices' && mode === 'operations' })
  const bizQ = useBusinessReport(rangeArgs, { enabled: domain === 'services' })
  const policyQ = useAvailabilityPolicy()
  const policyTarget = policyQ.data?.default_target_pct ?? null

  /* ── Current state ("now") ─────────────────────────────────────────── */
  const liveInterval = 15_000
  const deviceSummaryQ = useQuery<DeviceSummary>({
    queryKey: ['avail', 'summary'],
    queryFn: async () => (await api.get('/devices/summary')).data,
    refetchInterval: liveInterval,
  })
  const serviceSummaryQ = useQuery<ServiceSummary>({
    queryKey: ['avail', 'services'],
    queryFn: async () => (await api.get('/service-checks/summary')).data,
    refetchInterval: liveInterval,
  })
  const serversQ = useQuery<ServerMonitoringOverview>({
    queryKey: ['avail', 'servers'],
    queryFn: async () => (await api.get('/server-monitoring/overview')).data,
    refetchInterval: liveInterval,
    retry: 1,
  })
  // Complete scoped lists per status (not a 200-row inventory sample).
  const attentionQ = useQuery<{ down: DeviceLite[]; degraded: DeviceLite[]; maintenance: DeviceLite[] }>({
    queryKey: ['avail', 'attention'],
    queryFn: async () => {
      const get = async (status: string) =>
        ((await api.get('/devices', { params: { status, limit: 50 } })).data?.data || []) as DeviceLite[]
      const [down, degraded, maintenance] = await Promise.all([get('down'), get('degraded'), get('maintenance')])
      return { down, degraded, maintenance }
    },
    enabled: domain === 'devices',
    refetchInterval: liveInterval,
  })
  const attention = useMemo(
    () => [...(attentionQ.data?.down || []), ...(attentionQ.data?.degraded || [])],
    [attentionQ.data],
  )

  /* ── Period labelling ──────────────────────────────────────────────── */
  const fromMs = Date.parse(range.fromISO)
  const toMs = Date.parse(range.toISO)
  const periodLabel = PRESET_LABELS[range.key] ?? range.label
  const period = range.isCustom ? 'the selected period' : periodLabel.charAt(0).toLowerCase() + periodLabel.slice(1)
  const tz = useMemo(() => timezoneLabel(), [])

  const activeQ = domain === 'devices' ? execQ : domain === 'services' ? bizQ : null
  const activeData = activeQ?.data as { from?: string; to?: string; generated_at?: string; data_errors?: Array<{ source: string; message: string }> } | undefined
  const updatedAt = (domain === 'servers' ? serversQ.dataUpdatedAt : activeQ?.dataUpdatedAt) || null
  const generatedMs = parseTs(activeData?.generated_at ?? null)
  const skewMs = generatedMs != null && updatedAt ? updatedAt - generatedMs : null
  const applianceNow = nowTick - (skewMs ?? 0)
  const placeholder = !!activeQ?.isPlaceholderData
  const refreshFailed = !!activeQ && activeQ.isError && activeQ.data != null
  const prevFrom = parseTs(activeData?.from ?? null)
  const prevTo = parseTs(activeData?.to ?? null)
  const dataErrors = [
    ...(activeData?.data_errors || []),
    ...(domain === 'devices' && mode === 'operations' ? techQ.data?.data_errors || [] : []),
  ]

  // While the previous period is shown as a placeholder, sentences must name
  // the period the figures belong to, not the one being loaded.
  const assessedPeriod = placeholder && prevFrom != null && prevTo != null
    ? `the last loaded period (${fmtPeriod(prevFrom, prevTo)})`
    : period

  /* ── Shared assessment (A08) ───────────────────────────────────────── */
  const serviceRows = useMemo(() => normalizeServices(bizQ.data, policyTarget), [bizQ.data, policyTarget])
  const counts = useMemo(() => complianceCounts(serviceRows, bizQ.data), [serviceRows, bizQ.data])
  const assessment = useMemo(() => {
    if (domain === 'services') {
      return assessServices({
        business: bizQ.data, report: reportState(bizQ), live: serviceSummaryQ.data, rows: serviceRows, counts,
        policyTarget, period: assessedPeriod, nowMs: applianceNow,
      })
    }
    if (domain === 'servers') {
      return assessServers({ overview: serversQ.data, loading: serversQ.isLoading, error: serversQ.isError })
    }
    return assessDevices({
      exec: execQ.data, report: reportState(execQ), live: deviceSummaryQ.data, downNow: attention,
      policyTarget, period: assessedPeriod, nowMs: applianceNow,
    })
  }, [
    domain, bizQ.data, bizQ.isLoading, bizQ.isError, bizQ.isPlaceholderData, serviceSummaryQ.data, serviceRows, counts,
    policyTarget, assessedPeriod, applianceNow, serversQ.data, serversQ.isLoading, serversQ.isError,
    execQ.data, execQ.isLoading, execQ.isError, execQ.isPlaceholderData, deviceSummaryQ.data, attention,
  ])

  const loading = assessment.state === 'loading'
  const hardError = assessment.state === 'error'
  const common: ViewCommon = {
    assessment,
    periodLabel,
    period,
    fromMs,
    toMs,
    dimmed: placeholder,
    loading,
    large: noc,
    partial: dataErrors.length > 0,
  }

  const brief = (
    <BriefPanel brief={assessment.brief} period={domain === 'servers' ? 'Now' : placeholder ? 'Last loaded period' : periodLabel} dimmed={placeholder} loading={loading} />
  )

  const lastObsMs = domain === 'devices' ? parseTs(execQ.data?.coverage?.last_observation ?? null) : null

  const exportCsv = () => {
    const csv = buildAvailabilityCsv({
      domain,
      periodLabel,
      fromISO: range.fromISO,
      toISO: range.toISO,
      timezone: tz,
      generatedAt: activeData?.generated_at ?? null,
      assessment,
      exec: execQ.data,
      business: bizQ.data,
      serviceRows,
      servers: serversQ.data,
    })
    const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')
    downloadText(`availability-${domain}-${stamp}.csv`, csv)
  }
  const reportQuery = new URLSearchParams()
  for (const k of ['range', 'from', 'to']) {
    const v = params.get(k)
    if (v) reportQuery.set(k, v)
  }
  const formalReport = domain === 'devices'
    ? `/reports/executive${reportQuery.size ? `?${reportQuery}` : ''}`
    : domain === 'services'
      ? `/reports/business${reportQuery.size ? `?${reportQuery}` : ''}`
      : null

  const retry = () => {
    if (domain === 'servers') serversQ.refetch()
    else activeQ?.refetch()
  }

  return (
    <div className={cn('relative', noc && 'fixed inset-0 z-40 overflow-y-auto bg-bg p-4 md:p-6')}>
      <div className="mx-auto flex max-w-[1600px] flex-col gap-4 animate-fade-in">
        {/* Header */}
        <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className={cn('font-semibold tracking-tight text-text', noc ? 'text-3xl' : 'text-2xl')}>Availability overview</h1>
              <StreamBadge state={stream.connection} lastEventAt={stream.lastEventAt} />
              {activeQ?.isFetching && !loading && (
                <span className="inline-flex items-center gap-1.5 text-sm text-muted">
                  <Loader2 aria-hidden className="h-3.5 w-3.5 animate-spin" /> Updating
                </span>
              )}
            </div>
            <p className="mt-1 text-base text-text2">
              <span className="font-medium">{domain === 'servers' ? 'Current snapshot' : periodLabel}</span>
              {domain !== 'servers' && Number.isFinite(fromMs) && Number.isFinite(toMs) && <> · {fmtPeriod(fromMs, toMs)}</>}
              {' '}· {tz}
            </p>
            <p className="mt-0.5 text-sm text-muted">
              {domain === 'servers' ? 'Snapshot updated' : 'Updated'} {updatedAt ? fmtClock(updatedAt, true) : '—'}
              {domain === 'devices' && (
                <> · Last observation {lastObsMs != null ? `${fmtAge(lastObsMs, applianceNow)} (${fmtDateTime(lastObsMs)})` : 'not reported'}</>
              )}
              {noc && <NocClock />}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2 print:hidden">
            <TimeRangePicker
              rangeIdx={rangeIdx}
              isCustom={isCustom}
              customFrom={range.fromISO}
              customTo={range.toISO}
              onPreset={setPreset}
              onCustom={setCustom}
              presetLabels={PRESET_BUTTON_LABELS}
              calendarPresets
              activeKey={range.key}
              onCalendar={setCalendar}
            />
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="sm" className="h-9 text-sm">
                  <Download className="h-4 w-4" /> Export
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent>
                <DropdownMenuLabel>{periodLabel}</DropdownMenuLabel>
                <DropdownMenuItem onSelect={exportCsv} disabled={loading || hardError}>
                  <FileText /> CSV evidence (period, target, basis, coverage)
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => window.setTimeout(() => window.print(), 50)}>
                  <Printer /> Print or save as PDF
                </DropdownMenuItem>
                {formalReport && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem asChild>
                      <Link to={formalReport}><ExternalLink /> Open formal report</Link>
                    </DropdownMenuItem>
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
            <Button
              variant="outline"
              size="icon"
              onClick={() => setNoc((v) => !v)}
              title={noc ? 'Exit wall display (Esc)' : 'Wall display (NOC fullscreen)'}
              aria-label={noc ? 'Exit wall display' : 'Wall display'}
            >
              {noc ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
            </Button>
          </div>
        </div>

        {/* Scope + view controls */}
        <div className="flex flex-col gap-2 md:flex-row md:flex-wrap md:items-center md:justify-between print:hidden">
          <div className="flex flex-wrap items-center gap-2">
            <DomainTabs
              value={domain}
              onChange={(d) => setParam('view', d === 'devices' ? null : d)}
              devices={deviceSummaryQ.data}
              services={serviceSummaryQ.data}
              servers={serversQ.data}
            />
            <ModeTabs value={mode} onChange={(m) => setParam('mode', m === 'executive' ? null : m)} />
          </div>
          {domain !== 'servers' && (
            <PolicyControl
              effectiveTarget={assessment.target}
              label={domain === 'services' ? assessment.targetLabel : undefined}
            />
          )}
        </div>

        {/* Evidence and freshness notices */}
        {skewMs != null && Math.abs(skewMs) > SKEW_WARN_MS && (
          <Notice tone="risk">
            Clock disagreement: this browser is {fmtSkew(skewMs)} {skewMs > 0 ? 'ahead of' : 'behind'} the appliance
            (appliance time {fmtDateTime(generatedMs, true)}). Rolling periods use the browser clock; check NTP on the
            appliance and this device before relying on times.
          </Notice>
        )}
        {placeholder && prevFrom != null && prevTo != null && (
          <Notice tone="unknown" icon={<Loader2 aria-hidden className="h-4 w-4 animate-spin" />}>
            Showing the last loaded period ({fmtPeriod(prevFrom, prevTo)}) while {periodLabel.toLowerCase()} loads. Dimmed figures are not for the selected period.
          </Notice>
        )}
        {refreshFailed && !placeholder && (
          <Notice tone="risk" action={<button type="button" className="font-medium underline" onClick={retry}>Retry</button>}>
            Could not refresh. Showing data loaded at {activeQ?.dataUpdatedAt ? fmtClock(activeQ.dataUpdatedAt, true) : 'an earlier time'}.
          </Notice>
        )}
        {domain === 'servers' && (
          <Notice tone="neutral">
            Servers are shown as a current health snapshot. The selected period does not apply — historical server availability is not measured.
          </Notice>
        )}
        <DataErrorsBanner errors={dataErrors} />

        {hardError ? (
          <ErrorPanel
            title={`${DOMAINS.find((d) => d.key === domain)?.label} availability could not be loaded`}
            message="No figures are shown rather than presenting missing data as zero or as healthy."
            onRetry={retry}
          />
        ) : (
          <>
            {mode === 'operations' && <DecisionMetrics assessment={assessment} loading={loading} dimmed={placeholder} large={noc} />}

            {domain === 'devices' && mode === 'executive' && (
              <DevicesDashboard c={common} exec={execQ.data} live={deviceSummaryQ.data} brief={brief} />
            )}
            {domain === 'devices' && mode === 'operations' && (
              <DevicesOperations
                c={common}
                exec={execQ.data}
                technical={techQ.data}
                technicalLoading={techQ.isLoading}
                live={deviceSummaryQ.data}
                attention={attention}
                maintenance={attentionQ.data?.maintenance || []}
              />
            )}
            {domain === 'services' && mode === 'executive' && (
              <ServicesDashboard c={common} business={bizQ.data} rows={serviceRows} counts={counts} live={serviceSummaryQ.data} brief={brief} />
            )}
            {domain === 'services' && mode === 'operations' && (
              <ServicesOperations c={common} business={bizQ.data} rows={serviceRows} live={serviceSummaryQ.data} />
            )}
            {domain === 'servers' && mode === 'executive' && <ServersDashboard c={common} overview={serversQ.data} brief={brief} />}
            {domain === 'servers' && mode === 'operations' && <ServersOperations overview={serversQ.data} />}

            <p className="text-sm text-muted">
              Basis: {assessment.basis} Configured target: {assessment.targetLabel}.
            </p>
          </>
        )}
      </div>
    </div>
  )
}

/* ── Header pieces ─────────────────────────────────────────────────────── */

function StreamBadge({ state, lastEventAt }: { state: SSEConnectionState; lastEventAt: number | null }) {
  const meta = {
    live: { tone: 'met' as const, text: 'Live' },
    delayed: { tone: 'risk' as const, text: 'Delayed' },
    lost: { tone: 'breach' as const, text: 'Connection lost' },
    connecting: { tone: 'unknown' as const, text: 'Connecting' },
  }[state]
  const t = TONE[meta.tone]
  return (
    <span
      role="status"
      className={cn('inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-sm font-medium', t.border, t.bg, t.text)}
      title={`Status update stream${lastEventAt ? ` · last message ${fmtAge(lastEventAt)}` : ''}. The stream refreshes live counts; it does not prove fresh collection.`}
    >
      <span className="relative flex h-2 w-2">
        {state === 'live' && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-60 motion-reduce:hidden" />}
        <span className={cn('relative inline-flex h-2 w-2 rounded-full', t.dot)} />
      </span>
      {meta.text}
    </span>
  )
}

function NocClock() {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(id)
  }, [])
  return <> · Browser time <span className="font-mono tabular-nums text-text2">{fmtClock(now, true)}</span></>
}

function Notice({
  tone, children, icon, action,
}: {
  tone: 'risk' | 'unknown' | 'neutral'
  children: React.ReactNode
  icon?: React.ReactNode
  action?: React.ReactNode
}) {
  const t = TONE[tone]
  return (
    <div role="status" className={cn('flex items-start gap-2 rounded-lg border px-3 py-2 text-sm', t.border, t.bg, tone === 'neutral' ? 'text-text2' : t.text)}>
      {icon ?? (tone === 'risk' ? <TriangleAlert aria-hidden className="mt-0.5 h-4 w-4 shrink-0" /> : <ToneIcon tone={tone} className="mt-0.5" />)}
      <div className="min-w-0 flex-1">{children}</div>
      {action}
    </div>
  )
}

function DomainTabs({
  value, onChange, devices, services, servers,
}: {
  value: FleetDomain
  onChange: (d: FleetDomain) => void
  devices?: DeviceSummary
  services?: ServiceSummary
  servers?: ServerMonitoringOverview
}) {
  const now: Record<FleetDomain, string | null> = {
    devices: devices ? `${devices.up}/${devices.total} up` : null,
    services: services ? `${services.up}/${services.total} up` : null,
    servers: servers ? `${servers.status_counts?.healthy ?? 0}/${servers.total} healthy` : null,
  }
  const issues: Record<FleetDomain, number> = {
    devices: devices?.down ?? 0,
    services: services?.down ?? 0,
    servers: (servers?.status_counts?.critical ?? 0),
  }
  return (
    <div role="group" aria-label="Availability scope" className="flex w-full items-center gap-1 rounded-lg border border-border bg-surface p-1 sm:inline-flex sm:w-auto">
      {DOMAINS.map(({ key, label, icon: Icon }) => {
        const active = value === key
        return (
          <button
            key={key}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(key)}
            className={cn(
              'inline-flex min-h-[40px] flex-1 items-center justify-center gap-2 rounded-md px-2 text-base font-medium transition-colors sm:flex-none sm:px-3',
              active ? 'bg-primary text-white' : 'text-text2 hover:bg-surface2 hover:text-text',
            )}
          >
            <Icon aria-hidden className="hidden h-4 w-4 sm:block" />
            {label}
            {now[key] && (
              <span className={cn('hidden text-sm tabular-nums sm:inline', active ? 'text-white/85' : 'text-muted')} title="Current state (now)">
                · {now[key]}
              </span>
            )}
            {issues[key] > 0 && (
              <span className={cn('whitespace-nowrap rounded px-1.5 text-xs font-semibold', active ? 'bg-white/20 text-white' : 'bg-danger/10 text-danger')}>
                {issues[key]} {key === 'servers' ? 'critical' : 'down'}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}

function ModeTabs({ value, onChange }: { value: ViewMode; onChange: (m: ViewMode) => void }) {
  return (
    <div role="group" aria-label="View" className="inline-flex items-center gap-1 rounded-lg border border-border bg-surface p-1">
      {(['executive', 'operations'] as const).map((m) => (
        <button
          key={m}
          type="button"
          aria-pressed={value === m}
          onClick={() => onChange(m)}
          className={cn(
            'min-h-[40px] rounded-md px-3 text-base font-medium transition-colors',
            value === m ? 'bg-surface2 text-text ring-1 ring-border' : 'text-text2 hover:bg-surface2 hover:text-text',
          )}
        >
          {m === 'executive' ? 'Executive' : 'Operations'}
        </button>
      ))}
    </div>
  )
}
