import { useId, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bar, BarChart, CartesianGrid, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { ChevronDown, ChevronRight, History, Loader2, Search, UserSearch, Waypoints } from 'lucide-react'
import { api } from '@/lib/api'
import { toast } from '@/components/ui/Toast'
import { Button } from '@/components/ui/Button'
import { Textarea } from '@/components/ui/Textarea'
import { Input } from '@/components/ui/Input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { nf, nfKeys } from '@/lib/netflow/api'
import { formatBps, formatBytes, formatCount, formatDateTime } from '@/lib/netflow/format'
import { apiErrorMessage } from '@/lib/netflow/status'
import { cn, relativeTime } from '@/lib/utils'
import {
  ACTION_LABEL,
  CONFIDENCE_LABEL,
  allowedActions,
  evidenceFilter,
  evidenceWindow,
  hostProfileLink,
  hostTrafficLink,
  investigateLink,
  latestLink,
  observedPairs,
  subjectText,
  thresholdPhrases,
  timelineValue,
  type Finding,
  type LifecycleAction,
  type TimelinePoint,
  type TimelineResponse,
} from './model'
import { Field, FindingStatusChip, SeverityChip } from './parts'
import { chartFont } from '@/lib/chartTheme'

export interface FindingCardProps {
  finding: Finding
  /** The same detector and subject seen by other exporters (FE-12: one card per subject). */
  others?: Finding[]
  tz: string
  perms: { acknowledge: boolean; suppress: boolean }
  /** Selection covers the finding and its `others`. */
  selected?: boolean
  onSelect?: (on: boolean) => void
  /** Link that opens one finding by id on this page (`?finding=`). */
  focusLink?: (id: string) => string
  defaultOpen?: boolean
}

const LINK_CLASS =
  'inline-flex min-h-[32px] items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1 text-xs font-medium text-text hover:bg-surface2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50'

/** One finding: what matched, when, on which exporter, and how to investigate it (NF-09). */
export function FindingCard({ finding: f, others = [], tz, perms, selected, onSelect, focusLink, defaultOpen }: FindingCardProps) {
  const [open, setOpen] = useState(!!defaultOpen)
  const detailId = useId()
  const investigate = investigateLink(f)
  const selectable = !!onSelect
  return (
    <li
      className={cn(
        'rounded-lg border border-border border-l-4 bg-surface p-3',
        f.severity === 'critical' ? 'border-l-danger' : f.severity === 'warning' ? 'border-l-warning' : 'border-l-info',
      )}
    >
      <div className="flex items-start gap-2">
        {selectable && (
          <input
            type="checkbox"
            className="mt-1 h-4 w-4 shrink-0 accent-[rgb(var(--primary))]"
            checked={!!selected}
            onChange={(e) => onSelect?.(e.target.checked)}
            aria-label={`Select finding ${f.title} ${subjectText(f)}${others.length ? ` and the same on ${others.length} other exporter${others.length === 1 ? '' : 's'}` : ''}`}
          />
        )}
        <div className="flex min-w-0 flex-1 flex-col gap-2 min-[900px]:flex-row min-[900px]:items-start">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-1.5">
              <SeverityChip severity={f.severity} />
              <h3 className="text-sm font-semibold text-text">{f.title}</h3>
              <FindingStatusChip status={f.status} />
              <span className="rounded-md bg-surface2 px-1.5 py-0.5 text-xs font-medium text-text2">{f.label}</span>
            </div>
            <p className="mt-1 break-words font-mono text-sm text-text">
              {subjectText(f)}
              <span className="ml-2 font-sans text-xs text-text2">via exporter <span className="font-mono text-text">{f.exporter ?? '—'}</span></span>
            </p>
            <p className="mt-0.5 break-words text-sm leading-snug text-text2">{f.description}</p>
            <p className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-text2">
              <span>
                Last seen <span className="text-text">{formatDateTime(f.last_seen, tz, { seconds: false })}</span> ({relativeTime(f.last_seen)})
              </span>
              <span>
                First seen <span className="text-text">{formatDateTime(f.first_seen, tz, { seconds: false })}</span>
              </span>
              <span title="5-minute periods with activity">
                <span className="text-text">{formatCount(f.occurrences)}</span> occurrence{f.occurrences === 1 ? '' : 's'}
              </span>
              {f.confidence && <span>{CONFIDENCE_LABEL[f.confidence]}</span>}
              {f.assigned_to && (
                <span>
                  Assigned to <span className="text-text">{f.assigned_to.username ?? f.assigned_to.id}</span>
                </span>
              )}
            </p>
            {f.suppression && (
              <p className="mt-1 text-xs text-text2">
                <span className="font-medium text-text">Suppressed</span>
                {f.suppression.until ? ` until ${formatDateTime(f.suppression.until, tz, { seconds: false })}` : ''}
                {f.suppression.reason ? ` — ${f.suppression.reason}` : ''}
              </p>
            )}
          </div>
          <div className="flex shrink-0 flex-wrap gap-1.5 min-[900px]:flex-col min-[900px]:items-stretch">
            {investigate && (
              <Link to={investigate} className={LINK_CLASS} title="Raw flows behind this finding: its detector filter over its evidence window">
                <Search className="h-3.5 w-3.5" aria-hidden /> Investigate
              </Link>
            )}
            <button
              type="button"
              className={LINK_CLASS}
              aria-expanded={open}
              aria-controls={detailId}
              onClick={() => setOpen((o) => !o)}
            >
              {open ? <ChevronDown className="h-3.5 w-3.5" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5" aria-hidden />}
              {open ? 'Hide details' : 'Details'}
            </button>
          </div>
        </div>
      </div>
      {others.length > 0 && <OtherExporters others={others} tz={tz} focusLink={focusLink} />}
      {open && (
        <div id={detailId} className="mt-3 border-t border-border pt-3">
          <FindingDetail finding={f} tz={tz} perms={perms} />
        </div>
      )}
    </li>
  )
}

/**
 * Findings are per collection point (each carries its own exporter's evidence); the same activity
 * seen by several exporters is listed once, with the other exporters' findings under it.
 */
function OtherExporters({ others, tz, focusLink }: { others: Finding[]; tz: string; focusLink?: (id: string) => string }) {
  const [show, setShow] = useState(false)
  const listId = useId()
  const names = others.map((o) => o.exporter ?? '—')
  return (
    <div className="mt-2 rounded-md border border-border bg-surface2/50 px-2.5 py-1.5 text-xs">
      <button
        type="button"
        className="flex w-full items-start gap-1.5 text-left text-text2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
        aria-expanded={show}
        aria-controls={listId}
        onClick={() => setShow((v) => !v)}
      >
        {show ? <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden /> : <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />}
        <span>
          Also seen by {others.length} other exporter{others.length === 1 ? '' : 's'}:{' '}
          {names.slice(0, 3).map((n, i) => (
            <span key={`${n}-${i}`} className="whitespace-nowrap font-mono text-text">
              {n}
              {i < Math.min(3, names.length) - 1 ? ', ' : ''}
            </span>
          ))}
          {names.length > 3 && <span className="whitespace-nowrap"> and {names.length - 3} more</span>}
        </span>
      </button>
      {show && (
        <ul id={listId} className="mt-1.5 divide-y divide-border">
          {others.map((o) => {
            const inv = investigateLink(o)
            return (
              <li key={o.id} className="flex flex-col gap-1 py-1.5 min-[640px]:flex-row min-[640px]:items-center min-[640px]:justify-between">
                <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
                  <span className="font-mono text-text">{o.exporter ?? '—'}</span>
                  <FindingStatusChip status={o.status} />
                  <span className="text-text2">
                    last {formatDateTime(o.last_seen, tz, { seconds: false })} · {formatCount(o.occurrences)} occurrence{o.occurrences === 1 ? '' : 's'}
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {inv && (
                    <Link to={inv} className={LINK_CLASS} aria-label={`Investigate the finding on exporter ${o.exporter ?? ''}`}>
                      <Search className="h-3.5 w-3.5" aria-hidden /> Investigate
                    </Link>
                  )}
                  {o.persisted && focusLink && (
                    <Link to={focusLink(o.id)} className={LINK_CLASS} aria-label={`Open the finding on exporter ${o.exporter ?? ''}`}>
                      Open
                    </Link>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

/** Broader investigation paths: latest occurrence only, all traffic of the host, host profile. */
function MoreLinks({ f }: { f: Finding }) {
  const latest = latestLink(f)
  const hostLink = hostTrafficLink(f)
  const profile = hostProfileLink(f)
  const host = f.host ?? f.src ?? f.dst
  if (!latest && !hostLink && !profile) return null
  return (
    <div className="flex flex-wrap gap-1.5">
      {latest && f.occurrences !== null && f.occurrences > 1 && (
        <Link to={latest} className={LINK_CLASS} title="Only the latest occurrence">
          <History className="h-3.5 w-3.5" aria-hidden /> Latest occurrence only
        </Link>
      )}
      {hostLink && host && (
        <Link to={hostLink} className={LINK_CLASS} title={`Every flow to or from ${host} in the same window, any protocol`}>
          <Waypoints className="h-3.5 w-3.5" aria-hidden /> All traffic for {host}
        </Link>
      )}
      {profile && host && (
        <Link to={profile} className={LINK_CLASS} title={`Host profile of ${host}`}>
          <UserSearch className="h-3.5 w-3.5" aria-hidden /> Host profile
        </Link>
      )}
    </div>
  )
}

function FindingDetail({ finding, tz, perms }: { finding: Finding; tz: string; perms: FindingCardProps['perms'] }) {
  const persistedId = finding.persisted ? finding.id : finding.persisted_id ?? null
  const detail = useQuery({
    queryKey: nfKeys.query(`/netflow/findings/${persistedId}`),
    queryFn: async () => (await nf.get<Finding>(`/netflow/findings/${persistedId}`)).data,
    enabled: !!persistedId && finding.persisted,
  })
  const f = detail.data ?? finding
  const phrases = thresholdPhrases(f.threshold)
  const observed = observedPairs(f.observed)
  const win = evidenceWindow(f)
  const filterKeys = Object.entries(evidenceFilter(f))
  const feeds = (f.metrics?.feeds as { name: string; category: string }[] | undefined) ?? []
  const internal = (f.metrics?.internal_hosts as { ip: string; bytes: number; sent_bytes?: number; outbound: boolean }[] | undefined) ?? []
  return (
    <div className="space-y-4">
      <MoreLinks f={f} />
      {persistedId ? (
        <FindingTimeline id={persistedId} tz={tz} kind={f.kind} />
      ) : (
        <p className="text-xs text-text2">This match comes from an ad-hoc run and is not stored; its timeline is available once the detection engine records it.</p>
      )}
      <dl className="grid grid-cols-1 gap-3 min-[560px]:grid-cols-2">
        <Field label="Method">{f.method ?? '—'}</Field>
        <Field label="Judged against">{phrases.length ? phrases.join('; ') : '—'}</Field>
        <Field label="Evidence window">
          {win ? `${formatDateTime(win.from, tz, { seconds: false })} → ${formatDateTime(win.to, tz, { seconds: false })}` : '—'}
          {f.evidence?.truncated_span && <span className="block text-xs text-text2">Latest 31 days of a longer finding</span>}
        </Field>
        <Field label="Detector filter">
          {filterKeys.length ? filterKeys.map(([k, v]) => `${k}=${String(v)}`).join(' · ') : '—'}
        </Field>
        {f.baseline ? (
          <Field label="Baseline">
            {typeof f.baseline.p95_pps === 'number'
              ? `${(f.baseline.p95_pps as number).toFixed(1)} pps (p95 over ${String(f.baseline.hours)} h of history)`
              : typeof f.baseline.rate_bps === 'number'
                ? `${formatBps(f.baseline.rate_bps as number)} (${f.baseline.basis === 'hour_of_week_median' ? 'same hour of the week, 4-week median' : '7-day p95'})`
                : JSON.stringify(f.baseline)}
          </Field>
        ) : (
          <Field label="Baseline">None used{f.confidence === 'low' ? ' — too little history (low confidence)' : ''}</Field>
        )}
        {feeds.length > 0 && (
          <Field label="Threat-intel feeds">{feeds.map((x) => `${x.name} (${x.category})`).join(', ')}</Field>
        )}
      </dl>
      {observed.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-text">Observed</h4>
          <dl className="mt-1 grid grid-cols-1 gap-x-4 gap-y-1 text-xs min-[480px]:grid-cols-2 lg:grid-cols-3">
            {observed.map((o) => (
              <div key={o.key} className="min-w-0">
                <dt className="inline text-text2">{o.label}: </dt>
                <dd className="inline break-words text-text">{renderValue(o.key, o.value)}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
      {internal.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-text">Internal hosts involved</h4>
          <ul className="mt-1 flex flex-wrap gap-1.5">
            {internal.map((h) => (
              <li key={h.ip} className="rounded-md bg-surface2 px-2 py-0.5 font-mono text-xs text-text">
                {h.ip} · {formatBytes(h.bytes)} exchanged{h.outbound ? (typeof h.sent_bytes === 'number' ? ` · ${formatBytes(h.sent_bytes)} sent to it` : ' · sent to it') : ' · received only'}
              </li>
            ))}
          </ul>
        </div>
      )}
      {finding.persisted && <Lifecycle finding={f} perms={f.permissions ?? perms} tz={tz} />}
      {detail.data?.notes && detail.data.notes.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-text">History and notes</h4>
          <ol className="mt-1 space-y-1">
            {[...detail.data.notes].reverse().map((n) => (
              <li key={n.id} className="text-xs text-text2">
                <span className="text-text">{formatDateTime(n.at, tz, { seconds: false })}</span>
                {' · '}
                {n.by_name ?? (n.kind === 'system' ? 'Detection engine' : 'Unknown user')}
                {': '}
                <span className={n.kind === 'note' ? 'text-text' : undefined}>{n.text}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  )
}

function renderValue(key: string, v: unknown): string {
  if (Array.isArray(v)) return v.join(', ')
  if (typeof v === 'number') {
    if (key.includes('bytes')) return formatBytes(v)
    if (key.endsWith('_bps')) return formatBps(v)
    return Number.isInteger(v) ? v.toLocaleString('en-US') : String(Math.round(v * 10) / 10)
  }
  return String(v)
}

function FindingTimeline({ id, tz, kind }: { id: string; tz: string; kind: string }) {
  const q = useQuery({
    queryKey: nfKeys.query(`/netflow/findings/${id}/timeline`),
    queryFn: async () => (await nf.get<TimelineResponse>(`/netflow/findings/${id}/timeline`)).data,
  })
  return (
    <section aria-label="Evidence timeline">
      <QueryPanel query={q} label="the evidence timeline" skeleton="chart" compact isEmpty={(d) => !d.points.length}>
        {(d) => <TimelineChart data={d} tz={tz} kind={kind} />}
      </QueryPanel>
    </section>
  )
}

const RATE_UNITS: [number, string][] = [[1e9, 'G'], [1e6, 'M'], [1e3, 'k'], [1, '']]

/** Axis ticks share one unit (named in the caption) so labels stay one short line. */
function axisScale(key: keyof TimelinePoint, peak: number): { div: number; unit: string } {
  if (key === 'bps' || key === 'bytes') {
    const [div, prefix] = RATE_UNITS.find(([d]) => peak >= d) ?? [1, '']
    return { div, unit: key === 'bps' ? `${prefix}bps` : `${prefix}B` }
  }
  if (key === 'pps') return { div: 1, unit: 'pps' }
  return { div: 1, unit: '' }
}

function tickText(v: number, div: number): string {
  const x = v / div
  if (x === 0) return '0'
  return Math.abs(x) >= 100 ? Math.round(x).toLocaleString('en-US') : String(Math.round(x * 10) / 10)
}

function metricFormat(key: keyof TimelinePoint, v: number): string {
  if (key === 'bps') return formatBps(v)
  if (key === 'bytes') return formatBytes(v)
  if (key === 'pps') return `${v >= 100 ? Math.round(v).toLocaleString('en-US') : v.toFixed(1)} pps`
  return v.toLocaleString('en-US')
}

function TimelineChart({ data, tz, kind }: { data: TimelineResponse; tz: string; kind: string }) {
  const key = data.metric.key
  const points = data.points.map((p) => ({ ...p, label: formatDateTime(p.t, tz, { seconds: false, timeOnly: true, zone: 'none' }), value: timelineValue(p, key) }))
  const gaps = points.filter((p) => p.covered === false).length
  const segs = data.segments
    .map((s) => ({
      from: points.findIndex((p) => Date.parse(p.t) + data.bucket_s * 1000 > Date.parse(s.from)),
      to: findLastIndex(points, (p) => Date.parse(p.t) < Date.parse(s.to)),
    }))
    .filter((s) => s.from >= 0 && s.to >= s.from)
  const thresholdPps = kind.startsWith('icmp_flood') && typeof data.threshold?.effective_pps === 'number' ? (data.threshold.effective_pps as number) : null
  const peak = points.reduce((m, p) => Math.max(m, p.value ?? 0), 0)
  const scale = axisScale(key, Math.max(peak, thresholdPps ?? 0))
  const bucket = data.bucket_s >= 3600 ? `${data.bucket_s / 3600} h` : `${data.bucket_s / 60} min`
  return (
    <figure>
      <figcaption className="mb-1 flex flex-wrap items-baseline justify-between gap-x-3 text-xs text-text2">
        <span className="font-semibold text-text">
          {data.metric.label} per {bucket}
          {scale.unit ? <span className="font-normal text-text2"> ({scale.unit})</span> : null}
        </span>
        <span>
          Peak {metricFormat(key, peak)} · shaded: detected periods{thresholdPps !== null ? ` · dashed: ${metricFormat('pps', thresholdPps)} threshold` : ''}
          {gaps > 0 ? ` · ${gaps} blank period${gaps === 1 ? '' : 's'}: nothing received from the exporter` : ''}
        </span>
      </figcaption>
      <div className="h-44 w-full" role="img" aria-label={`${data.metric.label} per ${bucket} from ${points[0]?.label ?? ''} to ${points[points.length - 1]?.label ?? ''}; peak ${metricFormat(key, peak)}`}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={points} margin={{ top: 6, right: 8, left: 0, bottom: 0 }} barCategoryGap={1}>
            <CartesianGrid stroke="rgb(var(--border))" strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="label" tick={{ fontSize: chartFont(10), fill: 'rgb(var(--text2))' }} tickLine={false} axisLine={false} interval="preserveStartEnd" minTickGap={36} />
            <YAxis tick={{ fontSize: chartFont(10), fill: 'rgb(var(--text2))' }} tickLine={false} axisLine={false} width={44} tickFormatter={(v: number) => tickText(v, scale.div)} />
            {segs.map((s, i) => (
              <ReferenceArea key={i} x1={points[s.from].label} x2={points[s.to].label} fill="rgb(var(--warning))" fillOpacity={0.12} strokeOpacity={0} ifOverflow="extendDomain" />
            ))}
            {thresholdPps !== null && <ReferenceLine y={thresholdPps} stroke="rgb(var(--danger))" strokeDasharray="4 3" />}
            <Tooltip
              cursor={{ fill: 'rgb(var(--surface2))' }}
              contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8, fontSize: chartFont(12), color: 'rgb(var(--text))' }}
              formatter={(v: number) => [metricFormat(key, v), data.metric.label]}
              labelFormatter={(_l, p) => (p?.[0]?.payload?.t ? formatDateTime(p[0].payload.t as string, tz, { seconds: false }) : '')}
            />
            <Bar dataKey="value" fill="rgb(var(--primary))" radius={[3, 3, 0, 0]} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <details className="mt-1 text-xs">
        <summary className="cursor-pointer text-text2 hover:text-text">Show as table</summary>
        <div className="mt-1 max-h-48 overflow-auto rounded-md border border-border">
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-surface2 text-text2">
              <tr>
                <th scope="col" className="px-2 py-1 font-medium">Time ({tz})</th>
                <th scope="col" className="px-2 py-1 text-right font-medium">{data.metric.label}</th>
                <th scope="col" className="px-2 py-1 text-right font-medium">Records</th>
              </tr>
            </thead>
            <tbody>
              {points.filter((p) => p.covered === false || (p.records ?? 0) > 0).map((p) => (
                <tr key={p.t} className="border-t border-border">
                  <td className="px-2 py-0.5 text-text">{formatDateTime(p.t, tz, { seconds: false, zone: 'none' })}</td>
                  {p.value === null ? (
                    <td colSpan={2} className="px-2 py-0.5 text-right text-text2">no data received</td>
                  ) : (
                    <>
                      <td className="px-2 py-0.5 text-right tabular-nums text-text">{metricFormat(key, p.value)}</td>
                      <td className="px-2 py-0.5 text-right tabular-nums text-text">{(p.records ?? 0).toLocaleString('en-US')}</td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  )
}

function findLastIndex<T>(arr: T[], pred: (x: T) => boolean): number {
  for (let i = arr.length - 1; i >= 0; i--) if (pred(arr[i])) return i
  return -1
}

/* ─── Lifecycle ────────────────────────────────────────────────────── */

interface ActionBody {
  action: LifecycleAction
  note?: string
  reason?: string
  hours?: number
  user_id?: string | null
}

export function useFindingAction(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: ActionBody) => (await api.post(`/netflow/findings/${id}/actions`, body)).data as Finding,
    onSuccess: (_d, body) => {
      toast.success(`${ACTION_LABEL[body.action]}: done`)
      void qc.invalidateQueries({ queryKey: nfKeys.all })
    },
    onError: (err) => toast.error('The change was not saved', apiErrorMessage(err)),
  })
}

const SUPPRESS_HOURS = [
  { hours: 24, label: '1 day' },
  { hours: 24 * 7, label: '7 days' },
  { hours: 24 * 30, label: '30 days' },
  { hours: 24 * 90, label: '90 days' },
]

function Lifecycle({ finding: f, perms, tz }: { finding: Finding; perms: { acknowledge: boolean; suppress: boolean }; tz: string }) {
  const actions = allowedActions(f.status, perms)
  const mutate = useFindingAction(f.id)
  const [dialog, setDialog] = useState<null | 'suppress' | 'note' | 'assign'>(null)
  const [note, setNote] = useState('')
  const [reason, setReason] = useState('')
  const [hours, setHours] = useState(24 * 7)
  const [assignee, setAssignee] = useState<string>(f.assigned_to?.id ?? '')
  const users = useQuery({
    // Only users who can open this finding (netflow.view + its exporter in scope) can be assigned.
    queryKey: ['netflow', 'findings-assignees', f.id],
    queryFn: async () =>
      (await api.get('/netflow/findings-assignees', { params: { finding: f.id } })).data as { id: string; username: string; full_name: string | null }[],
    enabled: dialog === 'assign',
  })
  if (!actions.length) {
    return <p className="text-xs text-text2">You can view this finding; acknowledging or suppressing it needs alerts.acknowledge or netflow.manage.</p>
  }
  const run = (body: ActionBody, after?: () => void) =>
    mutate.mutate(body, {
      onSuccess: () => {
        after?.()
        setDialog(null)
      },
    })
  const direct: LifecycleAction[] = ['acknowledge', 'resolve', 'reopen', 'unsuppress']
  return (
    <div>
      <h4 className="text-xs font-semibold text-text">Actions</h4>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        {actions.map((a) =>
          direct.includes(a) ? (
            <Button key={a} size="sm" variant={a === 'acknowledge' ? 'default' : 'outline'} disabled={mutate.isPending} onClick={() => run({ action: a })}>
              {mutate.isPending && mutate.variables?.action === a && <Loader2 className="animate-spin" aria-hidden />}
              {ACTION_LABEL[a]}
            </Button>
          ) : (
            <Button key={a} size="sm" variant="outline" disabled={mutate.isPending} onClick={() => setDialog(a as 'suppress' | 'note' | 'assign')}>
              {ACTION_LABEL[a]}
            </Button>
          ),
        )}
      </div>
      {f.acknowledged_by && f.acknowledged_at && (
        <p className="mt-1.5 text-xs text-text2">
          Acknowledged by {f.acknowledged_by.username ?? f.acknowledged_by.id} at {formatDateTime(f.acknowledged_at, tz, { seconds: false })}
        </p>
      )}
      <Dialog open={dialog !== null} onOpenChange={(o) => !o && setDialog(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{dialog === 'suppress' ? 'Suppress finding' : dialog === 'assign' ? 'Assign finding' : 'Add a note'}</DialogTitle>
            <DialogDescription>
              {dialog === 'suppress'
                ? 'A suppressed finding stays visible under Suppressed and keeps recording occurrences; it returns to open when the suppression expires. To approve a recurring pattern for every host, add a detector exception instead.'
                : dialog === 'assign'
                  ? 'The assignee is recorded on the finding and in the audit log.'
                  : 'Notes are kept with the finding and audited.'}
            </DialogDescription>
          </DialogHeader>
          {dialog === 'suppress' && (
            <div className="space-y-3">
              <label className="block text-xs font-medium text-text">
                Reason
                <Input className="mt-1" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} placeholder="e.g. approved penetration test" />
              </label>
              <fieldset>
                <legend className="text-xs font-medium text-text">For</legend>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {SUPPRESS_HOURS.map((h) => (
                    <label key={h.hours} className={cn('cursor-pointer rounded-md border px-2.5 py-1 text-xs', hours === h.hours ? 'border-primary bg-primary/10 text-text' : 'border-border text-text2')}>
                      <input type="radio" name="suppress-hours" className="sr-only" checked={hours === h.hours} onChange={() => setHours(h.hours)} />
                      {h.label}
                    </label>
                  ))}
                </div>
              </fieldset>
            </div>
          )}
          {dialog === 'note' && (
            <label className="block text-xs font-medium text-text">
              Note
              <Textarea className="mt-1" value={note} onChange={(e) => setNote(e.target.value)} maxLength={4000} />
            </label>
          )}
          {dialog === 'assign' && (
            <label className="block text-xs font-medium text-text">
              Assignee
              <select
                className="mt-1 h-9 w-full rounded-md border border-border bg-surface px-2 text-sm text-text"
                value={assignee}
                onChange={(e) => setAssignee(e.target.value)}
                disabled={users.isLoading}
              >
                <option value="">Nobody (unassign)</option>
                {(users.data ?? []).map((u) => (
                  <option key={u.id} value={u.id}>{u.full_name ? `${u.full_name} (${u.username})` : u.username}</option>
                ))}
              </select>
              {users.isError && <span className="mt-1 block text-danger-fg">{apiErrorMessage(users.error)}</span>}
            </label>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialog(null)}>Cancel</Button>
            <Button
              disabled={mutate.isPending || (dialog === 'suppress' && !reason.trim()) || (dialog === 'note' && !note.trim())}
              onClick={() => {
                if (dialog === 'suppress') run({ action: 'suppress', reason: reason.trim(), hours }, () => setReason(''))
                if (dialog === 'note') run({ action: 'note', note: note.trim() }, () => setNote(''))
                if (dialog === 'assign') run({ action: 'assign', user_id: assignee || null })
              }}
            >
              {mutate.isPending && <Loader2 className="animate-spin" aria-hidden />}
              {dialog === 'suppress' ? 'Suppress' : dialog === 'assign' ? 'Save' : 'Add note'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

