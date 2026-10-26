import type { ReactNode } from 'react'
import { AlertTriangle, ArrowDownLeft, ArrowUpRight, CircleHelp, OctagonAlert, CheckCircle2, type LucideIcon } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { toSearchParams, type NfParams } from '@/lib/netflow/api'
import { formatBps, formatBpsExact, formatPercent } from '@/lib/netflow/format'
import { apiErrorMessage } from '@/lib/netflow/status'
import { toast } from '@/components/ui/Toast'
import {
  headroomView,
  ROLE_LABELS,
  SOURCE_LABELS,
  STATE_LABELS,
  stateTextClass,
  type CapacityState,
  type Direction,
  type Headline,
  type InterfaceRole,
  type SpeedSource,
} from './model'

/** The in/out series colours (validated categorical pair, both themes). */
export const DIRECTION_COLORS: Record<Direction, string> = { in: '#0284c7', out: '#d97706' }

const STATE_ICONS: Record<CapacityState, LucideIcon> = {
  over: OctagonAlert,
  critical: OctagonAlert,
  warning: AlertTriangle,
  healthy: CheckCircle2,
  insufficient: CircleHelp,
  unknown: CircleHelp,
}

/** State word with icon, in the state's foreground token (never colour alone). */
export function StateBadge({ state, className }: { state: CapacityState; className?: string }) {
  const Icon = STATE_ICONS[state]
  return (
    <span className={cn('inline-flex items-center gap-1 whitespace-nowrap text-xs font-semibold', stateTextClass(state), className)}>
      <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
      {STATE_LABELS[state]}
    </span>
  )
}

export function DirectionBadge({ direction, className }: { direction: Direction; className?: string }) {
  const Icon = direction === 'in' ? ArrowDownLeft : ArrowUpRight
  return (
    <span
      className={cn('inline-flex items-center gap-0.5 rounded border border-border bg-surface2/60 px-1 text-2xs font-medium uppercase tracking-wide text-text2', className)}
      title={direction === 'in' ? 'In: received on this interface' : 'Out: transmitted by this interface'}
    >
      <Icon className="h-3 w-3" style={{ color: DIRECTION_COLORS[direction] }} aria-hidden />
      {direction}
    </span>
  )
}

export function RoleBadge({ role }: { role: InterfaceRole | null | undefined }) {
  if (!role) return null
  return (
    <span className="inline-flex items-center rounded border border-border bg-surface2/60 px-1.5 text-2xs font-medium text-text2">
      {ROLE_LABELS[role]}
    </span>
  )
}

export function speedText(bps: number | null | undefined, source?: SpeedSource): ReactNode {
  if (!bps) return <span className="text-text2">Unknown</span>
  return (
    <span title={`${formatBpsExact(bps)}${source ? ` from ${SOURCE_LABELS[source]}` : ''}`}>{formatBps(bps)}</span>
  )
}

/** Rate plus utilization, e.g. "73.1 Mbps · 73.1%". */
export function RateCell({
  bps,
  util,
  state,
  align = 'right',
}: {
  bps: number | null | undefined
  util: number | null | undefined
  state?: CapacityState
  align?: 'left' | 'right'
}) {
  return (
    <div className={cn('flex flex-col', align === 'right' ? 'items-end' : 'items-start')}>
      <span className="whitespace-nowrap text-sm font-medium tabular-nums text-text" title={formatBpsExact(bps)}>
        {formatBps(bps)}
      </span>
      <span className={cn('whitespace-nowrap text-xs tabular-nums', util == null ? 'text-text2' : state ? stateTextClass(state) : 'text-text2')}>
        {util == null ? 'util unknown' : `${formatPercent(util)} util`}
      </span>
    </div>
  )
}

/** Remaining capacity: text first, bar = remaining share (fuller = more room), NF-15. */
export function Headroom({ headline, compact }: { headline: Headline; compact?: boolean }) {
  const v = headroomView(headline)
  if (v.kind === 'over') {
    return (
      <div className="flex flex-col gap-0.5" title={v.title}>
        <span className="inline-flex items-center gap-1 text-xs font-semibold tabular-nums text-danger-fg">
          <OctagonAlert className="h-3.5 w-3.5 shrink-0" aria-hidden />
          {v.text}
        </span>
        {v.sub && <span className="text-xs font-medium tabular-nums text-danger-fg">{v.sub}</span>}
        {!compact && <span className="text-xs leading-snug text-text2">Check speed, duplicate export or sampling</span>}
      </div>
    )
  }
  if (v.kind !== 'ok' || v.barPct == null) {
    return (
      <div className="flex flex-col gap-0.5" title={v.title}>
        <span className="text-xs text-text2">{v.text}</span>
        {v.sub && <span className="text-xs tabular-nums text-text2">{v.sub}</span>}
      </div>
    )
  }
  const tone =
    headline.state === 'critical' ? 'bg-danger' : headline.state === 'warning' ? 'bg-warning' : 'bg-success'
  return (
    <div className="flex min-w-[7.5rem] flex-col gap-1" title={v.title}>
      <span className="text-xs tabular-nums text-text">{v.text}</span>
      <div
        className="h-1.5 w-full overflow-hidden rounded-full bg-surface3"
        role="meter"
        aria-label="Remaining capacity"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(v.barPct)}
      >
        <div className={cn('h-full rounded-full', tone)} style={{ width: `${v.barPct}%` }} />
      </div>
    </div>
  )
}

export async function downloadCsv(path: string, params: NfParams, filename: string): Promise<void> {
  try {
    const res = await api.get(path, { params: toSearchParams({ ...params, format: 'csv' }), responseType: 'blob', timeout: 120_000 })
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    toast.error('Export failed', apiErrorMessage(e))
  }
}

export const selectCls =
  'h-9 rounded-md border border-border bg-surface px-2 text-sm text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40'
