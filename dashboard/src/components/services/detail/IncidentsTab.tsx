/**
 * Incidents & alerts tab (B10, B11, B12, B14, B15, UI 2).
 */
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, Bell, BellOff, Check, CheckCircle2, Info, SearchX, XCircle } from 'lucide-react'
import { cn, relativeTime } from '@/lib/utils'
import { rangePhrase } from '@/components/TimeRangePicker'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { ServiceFailureReason } from '@/components/services/ServiceFailureReason'
import { useWindowAlerts, type AlertStatusFilter } from './alerts'
import { IncidentPanel } from './IncidentPanel'
import { cleanAlertMessage, exactTime, formatDur, isRecoveryAlert } from './format'
import type { MaintenanceWindow, Outage, ServiceAlert } from './types'

export type IncidentEvidence = {
  coveragePct: number | null
  sampleCount: number | null
  coveredSec: number | null
}

const ALERT_PAGE = 50

function Section({ title, subtitle, actions, children }: { title: string; subtitle?: React.ReactNode; actions?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold tracking-tight text-text">{title}</h3>
          {subtitle && <div className="mt-0.5 text-xs text-muted">{subtitle}</div>}
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {children}
    </div>
  )
}

function Empty({ icon: Icon, title, description, action }: { icon: typeof Info; title: string; description: string; action?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center px-4 py-10 text-center">
      <span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-primary/10 text-primary"><Icon className="h-5 w-5" /></span>
      <h4 className="mt-3 text-sm font-semibold text-text">{title}</h4>
      <p className="mt-1 max-w-md text-xs leading-relaxed text-muted">{description}</p>
      {action && <div className="mt-3">{action}</div>}
    </div>
  )
}

function Segmented<T extends string>({ value, options, onChange, label }: { value: T; options: Array<{ v: T; label: string }>; onChange: (v: T) => void; label: string }) {
  return (
    <div className="flex gap-0.5 rounded-md bg-surface2/60 p-0.5" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.v} type="button" aria-pressed={value === o.v} onClick={() => onChange(o.v)}
          className={cn('rounded px-2 py-1 text-xs font-medium transition-colors', value === o.v ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text')}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

export function IncidentsTab({
  checkId, intervalSec, timeoutSec, outages, total, source, loading, error, onRetry, canLoadMore, onLoadMore, historyTruncated,
  evidence, fromISO, toISO, rangeLabel, nowMs, maintenance, activeAlertCount, ruleCovered, onAckAll, ackPending, onWiden, onZoom,
}: {
  checkId: string
  intervalSec: number
  timeoutSec: number
  outages: Outage[]
  total: number
  source: 'server' | 'client'
  loading: boolean
  error: boolean
  onRetry: () => void
  canLoadMore: boolean
  onLoadMore: () => void
  historyTruncated: boolean
  evidence: IncidentEvidence
  fromISO: string
  toISO: string
  rangeLabel: string
  nowMs: number
  maintenance: MaintenanceWindow[]
  activeAlertCount: number | undefined
  ruleCovered?: boolean
  onAckAll: () => void
  ackPending: boolean
  onWiden: () => void
  onZoom: (fromISO: string, toISO: string) => void
}) {
  const [kind, setKind] = useState<'all' | 'down' | 'warn'>('all')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [alertStatus, setAlertStatus] = useState<AlertStatusFilter>('all')
  const [alertPage, setAlertPage] = useState(0)
  useEffect(() => { setAlertPage(0) }, [fromISO, toISO, alertStatus])
  useEffect(() => { setSelectedId(null) }, [fromISO, toISO])

  const alertsQ = useWindowAlerts(checkId, fromISO, toISO, alertStatus, alertPage, ALERT_PAGE, true)
  const alerts = alertsQ.data?.data || []
  const alertTotal = alertsQ.data?.meta?.total ?? alerts.length
  const pages = Math.max(1, Math.ceil(alertTotal / ALERT_PAGE))

  const filtered = kind === 'all' ? outages : outages.filter((o) => o.kind === kind)
  const selected = outages.find((o) => o.id === selectedId) || null
  // Elapsed incident time inside the selected window: incidents from the
  // appliance carry their real start, which can precede the window.
  const winFrom = Date.parse(fromISO)
  const winTo = Math.min(Date.parse(toISO), nowMs)
  const totalDown = outages.reduce((s, o) => {
    const a = Math.max(o.start, Number.isFinite(winFrom) ? winFrom : o.start)
    const b = Math.min(o.end ?? nowMs, Number.isFinite(winTo) ? winTo : nowMs)
    return s + Math.max(0, b - a)
  }, 0) / 1000
  const noEvidence = (evidence.coveragePct != null && evidence.coveragePct <= 0) || (evidence.sampleCount === 0 && (evidence.coveredSec ?? 0) <= 0)
  const partialEvidence = !noEvidence && evidence.coveragePct != null && evidence.coveragePct < 90

  const emptyOutages = () => {
    if (outages.length > 0) return <Empty icon={Info} title="Nothing matches this filter" description="Switch the filter back to All." />
    if (noEvidence) {
      return (
        <Empty icon={SearchX} title="No monitoring evidence in this window"
          description={`No probe results were recorded for ${rangePhrase(rangeLabel)}, so it cannot be said whether the service was healthy. Check that the monitor is enabled and its sensor is reporting, or choose another range.`}
          action={<Button variant="outline" size="sm" onClick={onWiden}>Widen to 30 days</Button>} />
      )
    }
    return (
      <Empty icon={CheckCircle2} title={`No incidents in ${rangePhrase(rangeLabel)}`}
        description={partialEvidence
          ? `No confirmed Down or Warning intervals in the observed time, but only ${evidence.coveragePct!.toFixed(1)}% of this window has monitoring evidence.`
          : 'No confirmed Down or Warning intervals in the observed time. Isolated failed probes that recovered before confirmation are shown on the Overview chart.'}
        action={<Button variant="outline" size="sm" onClick={onWiden}>Widen to 30 days</Button>} />
    )
  }

  return (
    <div className="space-y-4">
      <Section
        title="Incidents"
        subtitle={
          <span>
            {outages.length > 0
              ? `${total > outages.length ? `${outages.length} of ${total}` : outages.length} in ${rangePhrase(rangeLabel)} · ${formatDur(totalDown)} elapsed in window`
              : rangeLabel}
            {' · '}select one to investigate
            {evidence.coveragePct != null && ` · ${evidence.coveragePct.toFixed(1)}% of window observed`}
          </span>
        }
        actions={
          <Segmented label="Incident kind" value={kind} onChange={setKind}
            options={[{ v: 'all', label: 'All' }, { v: 'down', label: 'Down' }, { v: 'warn', label: 'Warning' }]} />
        }
      >
        {loading ? (
          <div className="py-10 text-center text-xs text-muted">Loading incidents…</div>
        ) : error ? (
          <div className="py-8 text-center text-xs text-danger">
            Could not load incidents for this window.
            <button type="button" onClick={onRetry} className="ml-1.5 font-medium underline">Retry</button>
          </div>
        ) : filtered.length === 0 ? emptyOutages() : (
          <>
            <Table>
              <THead>
                <Tr>
                  <Th>Status</Th>
                  <Th>Started</Th>
                  <Th>Ended</Th>
                  <Th className="text-right">Duration</Th>
                  <Th>Reason</Th>
                </Tr>
              </THead>
              <TBody>
                {filtered.map((o) => {
                  const end = o.end ?? nowMs
                  const ongoing = o.end == null
                  const isSel = o.id === selectedId
                  return (
                    <Tr key={o.id} className={cn('cursor-pointer', isSel && 'bg-primary/5')} onClick={() => setSelectedId(isSel ? null : o.id)}>
                      <Td>
                        <button type="button" aria-pressed={isSel} aria-label={`Investigate ${o.kind === 'down' ? 'Down' : 'Warning'} incident starting ${exactTime(o.start)}`}
                          onClick={(e) => { e.stopPropagation(); setSelectedId(isSel ? null : o.id) }} className="rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50">
                          <Badge variant={o.kind === 'down' ? 'danger' : 'warning'}>
                            {o.kind === 'down' ? <XCircle className="h-3 w-3" /> : <AlertTriangle className="h-3 w-3" />}
                            {o.kind === 'down' ? 'Down' : 'Warning'}
                          </Badge>
                        </button>
                      </Td>
                      <Td>
                        <div className="font-mono text-xs tabular-nums">{exactTime(o.start)}</div>
                        <div className="text-2xs text-muted">{o.clippedStart ? 'started before this window' : relativeTime(new Date(o.start).toISOString())}</div>
                      </Td>
                      <Td>
                        {ongoing ? (
                          <span className="inline-flex items-center gap-1.5 text-xs font-medium text-danger">
                            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-danger" /> Ongoing
                          </span>
                        ) : (
                          <>
                            <div className="font-mono text-xs tabular-nums">{exactTime(end)}</div>
                            <div className="text-2xs text-muted">
                              {o.clippedEnd ? 'continues past this window'
                                : o.endKind === 'gap' ? 'evidence stopped — recovery not observed'
                                  : o.endKind === 'escalated' ? 'escalated to Down'
                                    : relativeTime(new Date(end).toISOString())}
                            </div>
                          </>
                        )}
                      </Td>
                      <Td className="text-right font-mono text-xs font-medium tabular-nums">
                        {o.clippedStart || o.clippedEnd ? '≥ ' : ''}{formatDur(o.durationSec ?? (end - o.start) / 1000)}
                      </Td>
                      <Td><div className="max-w-[380px] text-muted"><ServiceFailureReason reason={o.reason} /></div></Td>
                    </Tr>
                  )
                })}
              </TBody>
            </Table>
            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-4 py-2 text-xs text-muted">
              <span>
                {source === 'server' ? 'Built by the appliance from the full status log.' : 'Rebuilt in the browser from status history.'}
                {historyTruncated && ' Status history for this window exceeded the request limit; older incidents may be missing — narrow the range.'}
              </span>
              {canLoadMore && <Button variant="outline" size="sm" className="h-7" onClick={onLoadMore}>Load more ({total - outages.length} older)</Button>}
            </div>
          </>
        )}
      </Section>

      {selected && (
        <IncidentPanel
          key={selected.id}
          checkId={checkId}
          outage={selected}
          intervalSec={intervalSec}
          timeoutSec={timeoutSec}
          nowMs={nowMs}
          maintenance={maintenance}
          onClose={() => setSelectedId(null)}
          onZoom={onZoom}
        />
      )}

      <Section
        title={`Alerts triggered in selected window${alertsQ.data ? ` (${alertTotal})` : ''}`}
        subtitle={
          <span>
            {rangeLabel}
            {activeAlertCount != null && ` · ${activeAlertCount} active for this monitor in total`}
          </span>
        }
        actions={
          <>
            <Segmented label="Alert status" value={alertStatus} onChange={setAlertStatus}
              options={[{ v: 'all', label: 'All' }, { v: 'active', label: 'Active' }, { v: 'acknowledged', label: 'Ack' }, { v: 'resolved', label: 'Resolved' }]} />
            <Button variant="outline" size="sm" className="h-7" onClick={onAckAll} disabled={!activeAlertCount || ackPending}
              title="Acknowledge every active alert for this monitor, including any outside the selected window">
              <Check className="h-3 w-3" /> Ack all active{activeAlertCount ? ` (${activeAlertCount})` : ''}
            </Button>
          </>
        }
      >
        {alertsQ.isLoading ? (
          <div className="py-10 text-center text-xs text-muted">Loading alerts…</div>
        ) : alertsQ.isError ? (
          <div className="py-8 text-center text-xs text-danger">
            Could not load alerts. <button type="button" onClick={() => alertsQ.refetch()} className="ml-1 font-medium underline">Retry</button>
          </div>
        ) : alerts.length === 0 ? (
          ruleCovered === false ? (
            <Empty icon={BellOff} title="No alert rule covers this check"
              description="Incidents are recorded above, but nothing will notify anyone: no enabled alert rule targets this check, its group, or the service_status metric."
              action={<Button variant="outline" size="sm" asChild><Link to="/alert-rules">Create an alert rule</Link></Button>} />
          ) : (
            <Empty icon={Bell}
              title={alertStatus === 'all' ? `No alerts triggered in ${rangePhrase(rangeLabel)}` : 'Nothing matches this filter'}
              description={alertStatus === 'all' ? 'Alerts raised for this check appear here when they are triggered inside the selected window.' : 'Switch the status filter back to All.'} />
          )
        ) : (
          <>
            <Table>
              <THead>
                <Tr>
                  <Th>Severity</Th>
                  <Th>Message</Th>
                  <Th>Status</Th>
                  <Th>Triggered</Th>
                  <Th>Resolved</Th>
                </Tr>
              </THead>
              <TBody>
                {alerts.map((a) => <AlertRow key={a.id} alert={a} />)}
              </TBody>
            </Table>
            {pages > 1 && (
              <div className="flex items-center justify-between gap-2 border-t border-border px-4 py-2 text-xs text-muted">
                <span>Page {alertPage + 1} of {pages} · {alertTotal} alerts</span>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" className="h-7" disabled={alertPage === 0 || alertsQ.isFetching} onClick={() => setAlertPage((p) => p - 1)}>Newer</Button>
                  <Button variant="outline" size="sm" className="h-7" disabled={alertPage + 1 >= pages || alertsQ.isFetching} onClick={() => setAlertPage((p) => p + 1)}>Older</Button>
                </div>
              </div>
            )}
          </>
        )}
      </Section>
    </div>
  )
}

function AlertRow({ alert: a }: { alert: ServiceAlert }) {
  const recovery = isRecoveryAlert(a)
  const sev = (a.severity || 'info').toLowerCase()
  const open = a.status === 'active' || a.status === 'pending'
  return (
    <Tr>
      <Td>
        {recovery ? (
          <Badge variant="success"><CheckCircle2 className="h-3 w-3" />Recovered</Badge>
        ) : open || a.status === 'acknowledged' ? (
          <Badge variant={sev === 'critical' ? 'danger' : sev === 'warning' ? 'warning' : 'info'}>{sev}</Badge>
        ) : (
          <Badge variant="outline" title="Original severity; the alert is resolved">{sev} · resolved</Badge>
        )}
      </Td>
      <Td>
        <Link to={`/alerts/${a.id}`} className="block max-w-[420px] truncate text-xs hover:text-primary hover:underline" title={cleanAlertMessage(a.message)}>
          {recovery ? 'Recovery notification · ' : ''}{cleanAlertMessage(a.message)}
        </Link>
      </Td>
      <Td>
        <span className={cn('text-xs font-medium capitalize',
          recovery ? 'text-success' : a.status === 'active' ? 'text-danger' : a.status === 'acknowledged' || a.status === 'pending' ? 'text-warning' : 'text-success')}>
          {recovery ? 'recovered' : a.status === 'pending' ? 'held' : a.status}
        </span>
      </Td>
      <Td className="text-xs tabular-nums text-muted">
        <div className="font-mono text-text2">{exactTime(a.triggered_at)}</div>
        <div>{relativeTime(a.triggered_at)}</div>
      </Td>
      <Td className="text-xs tabular-nums text-muted">
        {a.resolved_at ? <><div className="font-mono text-text2">{exactTime(a.resolved_at)}</div><div>{relativeTime(a.resolved_at)}</div></> : open ? 'Open' : '—'}
      </Td>
    </Tr>
  )
}
