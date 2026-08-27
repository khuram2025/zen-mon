import { type ReactNode } from 'react'
import { NavLink } from 'react-router-dom'
import type { LucideIcon } from 'lucide-react'
import { ChevronDown, ChevronRight, Monitor, Server, UserRound, Workflow } from 'lucide-react'
import { cn } from '@/lib/utils'
import { fmtMs } from '@/components/apm/shared'
import { fmtCount } from '@/components/apm/viz'

/** Avi-style underline tabs used by APM chrome and explorers. */
export function ApmUnderlineNav({
  items,
  className,
}: {
  items: Array<{
    key: string
    label: string
    to?: string
    end?: boolean
    icon?: LucideIcon
    count?: number
    current?: boolean
    onSelect?: () => void
    title?: string
  }>
  className?: string
}) {
  return (
    <nav className={cn('flex items-center gap-0 overflow-x-auto border-b border-border', className)}>
      {items.map((item) => {
        const Icon = item.icon
        const body = (
          <>
            {Icon && <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />}
            {item.label}
            {item.count != null && (
              <span className="font-mono text-[10px] tabular-nums opacity-70">{fmtCount(item.count)}</span>
            )}
          </>
        )
        const cls = (active: boolean) =>
          cn(
            '-mb-px inline-flex items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-[13px] font-medium transition-colors',
            active
              ? 'border-primary text-primary'
              : 'border-transparent text-muted hover:border-border hover:text-text',
          )
        if (item.to) {
          return (
            <NavLink key={item.key} to={item.to} end={item.end} title={item.title} className={({ isActive }) => cls(isActive)}>
              {body}
            </NavLink>
          )
        }
        return (
          <button
            key={item.key}
            type="button"
            title={item.title}
            aria-current={item.current ? 'page' : undefined}
            onClick={item.onSelect}
            className={cls(!!item.current)}
          >
            {body}
          </button>
        )
      })}
    </nav>
  )
}

/** Horizontal duration bar used in explorer tables (Avi timeline column). */
export function DurationTimeline({
  ms,
  maxMs,
  significant,
  className,
}: {
  ms: number
  maxMs: number
  significant?: boolean
  className?: string
}) {
  if (ms <= 0 || !Number.isFinite(ms)) {
    return (
      <div className={cn('min-w-[5.5rem]', className)}>
        <div className="text-right font-mono text-[11px] tabular-nums text-muted">—</div>
        <div className="mt-1 h-2 overflow-hidden rounded-sm bg-surface2" />
      </div>
    )
  }
  const pct = maxMs <= 0 ? 0 : Math.max(4, Math.min(100, (ms / maxMs) * 100))
  const slow = ms >= 1000
  const color = significant || slow ? '#f59e0b' : '#7c3aed'
  return (
    <div className={cn('min-w-[5.5rem]', className)}>
      <div className="text-right font-mono text-[11px] tabular-nums text-text">{fmtMs(ms)}</div>
      <div className="mt-1 h-2 overflow-hidden rounded-sm bg-surface2">
        <div className="h-full rounded-sm" style={{ width: `${pct}%`, backgroundColor: color }} />
      </div>
    </div>
  )
}

export type RequestHop = {
  id: string
  label: string
  hint?: string
  metric?: string
  status?: string | number | null
  tone?: 'ok' | 'warn' | 'err' | 'muted'
  icon?: LucideIcon
}

const HOP_TONE = {
  ok: 'border-success/40 bg-success/10 text-success',
  warn: 'border-warning/40 bg-warning/10 text-warning',
  err: 'border-danger/40 bg-danger/10 text-danger',
  muted: 'border-border bg-surface2 text-muted',
}

/** Client → service → app request path, inspired by NSX ALB log drill-down. */
export function RequestFlow({ hops, totalLabel }: { hops: RequestHop[]; totalLabel?: string }) {
  if (!hops.length) return null
  return (
    <div className="rounded-lg border border-border bg-surface2/30 px-4 py-3">
      <div className="flex flex-wrap items-stretch gap-0">
        {hops.map((hop, index) => {
          const Icon = hop.icon || (index === 0 ? UserRound : index === hops.length - 1 ? Workflow : Server)
          const tone = hop.tone || 'muted'
          return (
            <div key={hop.id} className="flex min-w-0 items-center">
              <div className="flex min-w-[7rem] flex-col items-center text-center">
                {hop.metric && <div className="mb-1 text-[10px] font-medium tabular-nums text-text2">{hop.metric}</div>}
                <span className={cn('flex h-9 w-9 items-center justify-center rounded-lg border', HOP_TONE[tone])}>
                  <Icon className="h-4 w-4" />
                </span>
                <div className="mt-1.5 text-[11px] font-semibold text-text">{hop.label}</div>
                {hop.hint && <div className="max-w-[8rem] truncate text-[10px] text-muted" title={hop.hint}>{hop.hint}</div>}
                {hop.status != null && hop.status !== '' && (
                  <span className={cn(
                    'mt-1 rounded px-1.5 py-px font-mono text-[10px]',
                    Number(hop.status) >= 400 ? 'bg-danger/15 text-danger' : 'bg-success/15 text-success',
                  )}>{hop.status}</span>
                )}
              </div>
              {index < hops.length - 1 && (
                <div className="mx-1 mb-4 h-px w-10 shrink-0 bg-border sm:w-14" aria-hidden />
              )}
            </div>
          )
        })}
        {totalLabel && (
          <div className="ml-auto flex flex-col items-end justify-center pl-4">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">Total time</div>
            <div className="text-lg font-semibold tabular-nums text-text">{totalLabel}</div>
          </div>
        )}
      </div>
    </div>
  )
}

export type FacetGroup = {
  title: string
  items: Array<{ value: string; count: number; active?: boolean; onSelect: () => void }>
}

/** Right-hand analytics pane: click a value to segment the explorer. */
export function ApmFacetSidebar({ title = 'Analytics', groups }: { title?: string; groups: FacetGroup[] }) {
  const visible = groups.filter((group) => group.items.length > 0)
  if (!visible.length) return null
  return (
    <aside className="rounded-lg border border-border bg-surface">
      <div className="border-b border-border px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</div>
      <div className="divide-y divide-border">
        {visible.map((group) => (
          <details key={group.title} open className="px-3 py-2">
            <summary className="cursor-pointer list-none text-[11px] font-semibold text-text2 [&::-webkit-details-marker]:hidden">
              <span className="inline-flex items-center gap-1">
                <ChevronDown className="h-3 w-3 text-muted" />
                {group.title}
              </span>
            </summary>
            <div className="mt-1.5 space-y-0.5">
              {group.items.slice(0, 8).map((item) => (
                <button
                  key={item.value}
                  type="button"
                  onClick={item.onSelect}
                  className={cn(
                    'flex w-full items-center justify-between gap-2 rounded px-1.5 py-1 text-left text-[11px]',
                    item.active ? 'bg-primary/15 text-text' : 'text-muted hover:bg-surface2 hover:text-text',
                  )}
                >
                  <span className="truncate">{item.value || 'Unknown'}</span>
                  <span className="font-mono tabular-nums">{fmtCount(item.count)}</span>
                </button>
              ))}
            </div>
          </details>
        ))}
      </div>
    </aside>
  )
}

export type HistogramBucket = { ok: number; err?: number }

/** Compact volume histogram sitting above an explorer table. */
export function VolumeHistogram({
  buckets,
  okLabel = 'Healthy',
  errLabel = 'Significant',
}: {
  buckets: HistogramBucket[]
  okLabel?: string
  errLabel?: string
}) {
  if (buckets.length < 2) return null
  const max = Math.max(...buckets.map((b) => b.ok + (b.err ?? 0)), 1)
  const hasErr = buckets.some((b) => (b.err ?? 0) > 0)
  return (
    <div className="border-b border-border px-3 pt-2 pb-1">
      <div className="flex h-14 items-end gap-px">
        {buckets.map((bucket, index) => {
          const err = bucket.err ?? 0
          const total = bucket.ok + err
          const height = Math.max(4, (total / max) * 100)
          return (
            <div key={index} className="flex min-w-0 flex-1 flex-col justify-end" style={{ height: `${height}%` }}>
              {err > 0 && <div className="w-full bg-warning" style={{ height: `${(err / total) * 100}%` }} />}
              {bucket.ok > 0 && <div className="w-full bg-success/70" style={{ height: `${(bucket.ok / total) * 100}%` }} />}
            </div>
          )
        })}
      </div>
      <div className="mt-1 flex items-center gap-3 text-[10px] text-muted">
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-sm bg-success/70" /> {okLabel}</span>
        {hasErr && <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-sm bg-warning" /> {errLabel}</span>}
      </div>
    </div>
  )
}

export function ApmExplorerFrame({
  search,
  actions,
  summary,
  histogram,
  sidebar,
  children,
}: {
  search?: ReactNode
  actions?: ReactNode
  summary?: ReactNode
  histogram?: ReactNode
  sidebar?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="space-y-3">
      {(search || actions) && (
        <div className="flex flex-wrap items-center gap-2">
          {search}
          <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div>
        </div>
      )}
      {summary && <div className="text-[11px] text-muted">{summary}</div>}
      <div className={cn('grid gap-3', sidebar && 'xl:grid-cols-[minmax(0,1fr),240px]')}>
        <div className="min-w-0 overflow-hidden rounded-lg border border-border bg-surface">
          {histogram}
          {children}
        </div>
        {sidebar}
      </div>
    </div>
  )
}

export const EXPLORER_HEAD = 'bg-surface3/80 [&_th]:h-9 [&_th]:text-[10px] [&_th]:font-semibold [&_th]:text-text2'

export function ExpandToggle({ open, onClick }: { open: boolean; onClick: () => void }) {
  const Icon = open ? ChevronDown : ChevronRight
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-label={open ? 'Collapse row' : 'Expand row'}
      onClick={(event) => { event.stopPropagation(); onClick() }}
      className="inline-flex h-6 w-6 items-center justify-center rounded border border-border text-muted hover:bg-surface2 hover:text-text"
    >
      <Icon className="h-3.5 w-3.5" />
    </button>
  )
}

export function defaultClientHop(hint?: string): RequestHop {
  return { id: 'client', label: 'Client', hint, icon: UserRound, tone: 'ok' }
}

export function monitorHop(label: string, hint?: string): RequestHop {
  return { id: 'monitor', label, hint, icon: Monitor, tone: 'muted' }
}

export type TraceSpanLike = {
  span_id: string
  parent_span_id?: string | null
  name: string
  service_name: string
  duration_ms: number
  has_error?: boolean
  http_status_code?: number | null
  db_system?: string
  db_operation?: string
  depth?: number
}

function hopStatus(code?: number | null): number | null {
  return code && code > 0 ? code : null
}

/** Build Client → service → downstream/app hops from a fetched trace. */
export function hopsFromTraceSpans(spans: TraceSpanLike[]): RequestHop[] {
  if (!spans.length) return [defaultClientHop()]
  const root = spans.find((span) => !span.parent_span_id) ?? spans.find((span) => span.depth === 0) ?? spans[0]
  const children = spans.filter((span) => span.parent_span_id === root.span_id)
  const longestChild = [...children].sort((a, b) => b.duration_ms - a.duration_ms)[0]
  const db = spans.find((span) => !!span.db_system)
  const hops: RequestHop[] = [
    defaultClientHop(),
    {
      id: 'entry',
      label: root.service_name || 'Service',
      hint: root.name,
      metric: fmtMs(root.duration_ms),
      status: hopStatus(root.http_status_code),
      icon: Server,
      tone: root.has_error ? 'err' : 'ok',
    },
  ]
  if (longestChild && longestChild.service_name && longestChild.service_name !== root.service_name) {
    hops.push({
      id: 'downstream',
      label: longestChild.service_name,
      hint: longestChild.name,
      metric: fmtMs(longestChild.duration_ms),
      status: hopStatus(longestChild.http_status_code),
      tone: longestChild.has_error ? 'err' : 'ok',
    })
  }
  if (db) {
    hops.push({
      id: 'store',
      label: db.db_system || 'App',
      hint: db.db_operation || db.name,
      metric: fmtMs(db.duration_ms),
      icon: Workflow,
      tone: db.has_error ? 'err' : 'muted',
    })
  } else if (hops.length < 3) {
    hops.push(monitorHop('App', longestChild?.name || `${spans.length} spans`))
  }
  return hops
}

export function hopsFromTraceSummary(trace: {
  root_service: string
  root_operation: string
  duration_ms: number
  span_count: number
  has_error: boolean
}): RequestHop[] {
  return [
    defaultClientHop(),
    {
      id: 'entry',
      label: trace.root_service || 'Service',
      hint: trace.root_operation,
      metric: fmtMs(trace.duration_ms),
      icon: Server,
      tone: trace.has_error ? 'err' : 'ok',
    },
    monitorHop('App', `${trace.span_count} spans`),
  ]
}

export function downloadCsv(filename: string, headers: string[], rows: Array<Array<string | number | null | undefined>>) {
  const cell = (value: string | number | null | undefined) => `"${String(value ?? '').replaceAll('"', '""')}"`
  const csv = [headers.map(cell).join(','), ...rows.map((row) => row.map(cell).join(','))].join('\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

export function bucketByTime<T>(
  items: T[],
  getTime: (item: T) => string,
  isErr: (item: T) => boolean,
  slots = 24,
): HistogramBucket[] {
  if (!items.length) return []
  const times = items.map((item) => new Date(getTime(item)).getTime()).filter((n) => Number.isFinite(n))
  if (times.length < 2) return items.map((item) => ({ ok: isErr(item) ? 0 : 1, err: isErr(item) ? 1 : 0 }))
  const min = Math.min(...times)
  const max = Math.max(...times)
  const span = Math.max(max - min, 1)
  const buckets = Array.from({ length: slots }, () => ({ ok: 0, err: 0 }))
  items.forEach((item) => {
    const t = new Date(getTime(item)).getTime()
    if (!Number.isFinite(t)) return
    const index = Math.min(slots - 1, Math.max(0, Math.floor(((t - min) / span) * slots)))
    if (isErr(item)) buckets[index].err += 1
    else buckets[index].ok += 1
  })
  return buckets
}
