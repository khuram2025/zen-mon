/**
 * Small building blocks shared by the Synthetics list and detail pages.
 */
import type { ReactNode } from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { CheckCircle2, CircleDashed, X, XCircle } from 'lucide-react'
import { Badge } from '@/components/ui/Badge'
import { fmtMs } from '@/components/apm/shared'
import { seriesVar } from '@/lib/chartTheme'
import { regionalDate } from '@/lib/regionalTime'
import { cn } from '@/lib/utils'
import {
  type ChipState, type SynTiming, type UptimeTone,
  fmtUptime, sparkBars, sparkBucketRange, timingSegments,
} from '@/features/apm/synthetics'

export function StatusPill({ status, enabled }: { status: string; enabled: boolean }) {
  if (!enabled) return <Badge variant="outline">Disabled</Badge>
  if (status === 'up') return <Badge variant="success"><CheckCircle2 className="h-3 w-3" aria-hidden /> Up</Badge>
  if (status === 'down') return <Badge variant="danger"><XCircle className="h-3 w-3" aria-hidden /> Down</Badge>
  return <Badge variant="outline">Pending</Badge>
}

export const TONE_BG: Record<UptimeTone, string> = {
  ok: 'bg-success/80',
  warn: 'bg-warning',
  bad: 'bg-danger',
  none: 'bg-surface2',
}

export const TONE_TEXT: Record<UptimeTone, string> = {
  ok: 'text-text2',
  warn: 'text-warning-fg',
  bad: 'text-danger-fg',
  none: 'text-muted',
}

/** 48 bars of availability across the selected range. */
export function AvailabilitySpark({ spark, from, to, className }: {
  spark?: ReadonlyArray<number | null> | null
  from?: string
  to?: string
  className?: string
}) {
  const bars = sparkBars(spark)
  return (
    <div className={cn('flex h-5 items-end gap-px', className)} role="img" aria-label="Availability over the selected range">
      {bars.map((b) => {
        const span = from && to ? sparkBucketRange(b.i, from, to) : null
        const when = span ? `${regionalDate(new Date(span.from), 'datetime')} → ${regionalDate(new Date(span.to), 'time')}` : ''
        return (
          <span
            key={b.i}
            title={`${when}${when ? ' — ' : ''}${b.pct == null ? 'no runs' : fmtUptime(b.pct)}`}
            className={cn('w-[3px] min-w-[2px] flex-1 rounded-[1px]', TONE_BG[b.tone], b.tone === 'none' && 'opacity-60')}
            style={{ height: `${b.height}%` }}
          />
        )
      })}
    </div>
  )
}

/** Average connect / TLS / wait / download split of a request, as one bar with a legend in its tooltip. */
export function TimingBar({ timing, className, showLegend }: { timing?: SynTiming | null; className?: string; showLegend?: boolean }) {
  const segs = timingSegments(timing)
  if (!segs.length) return <span className="text-xs text-muted">—</span>
  const title = segs.map((s) => `${s.label} ${fmtMs(s.ms)}`).join(' · ')
  return (
    <div className={cn('min-w-[7rem]', className)}>
      <div className="flex h-2 w-full overflow-hidden rounded-full bg-surface2" title={title} role="img" aria-label={title}>
        {segs.map((s) => (
          <span key={s.key} style={{ width: `${s.pct}%`, backgroundColor: seriesVar(s.series) }} />
        ))}
      </div>
      {showLegend && <TimingLegend timing={timing} />}
    </div>
  )
}

export function TimingLegend({ timing }: { timing?: SynTiming | null }) {
  const segs = timingSegments(timing)
  if (!segs.length) return null
  return (
    <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-2xs text-muted">
      {segs.map((s) => (
        <span key={s.key} className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-sm" style={{ backgroundColor: seriesVar(s.series) }} aria-hidden />
          {s.label} <span className="tabular-nums text-text2">{fmtMs(s.ms)}</span>
        </span>
      ))}
    </div>
  )
}

const CHIP: Record<ChipState, string> = {
  ok: 'border-success/40 bg-success/15 text-success-fg',
  fail: 'border-danger/50 bg-danger/15 text-danger-fg',
  skipped: 'border-border bg-surface2 text-muted',
}

/** One status chip per step of a run. */
export function StepChips({ chips }: { chips: Array<{ name: string; state: ChipState; status_code: number | null; ms: number | null }> }) {
  return (
    <div className="flex flex-wrap gap-1">
      {chips.map((c, i) => (
        <span
          key={i}
          title={`${i + 1}. ${c.name} — ${c.state === 'skipped' ? 'not run' : `${c.state === 'ok' ? 'passed' : 'failed'}${c.status_code ? ` · ${c.status_code}` : ''}${c.ms != null ? ` · ${fmtMs(c.ms)}` : ''}`}`}
          className={cn('inline-flex h-5 min-w-[1.25rem] items-center justify-center gap-0.5 rounded border px-1 text-2xs font-semibold tabular-nums', CHIP[c.state])}
        >
          {c.state === 'ok' ? <CheckCircle2 className="h-3 w-3" aria-hidden /> : c.state === 'fail' ? <XCircle className="h-3 w-3" aria-hidden /> : <CircleDashed className="h-3 w-3" aria-hidden />}
          {i + 1}
        </span>
      ))}
    </div>
  )
}

export function MethodTag({ method }: { method: string }) {
  return (
    <span className="inline-flex shrink-0 rounded border border-border bg-surface2 px-1 font-mono text-2xs font-semibold text-text2">
      {(method || 'GET').toUpperCase()}
    </span>
  )
}

export function StatusCode({ code }: { code: number | null | undefined }) {
  if (!code) return <span className="text-muted">—</span>
  const tone = code >= 500 ? 'text-danger-fg' : code >= 400 ? 'text-warning-fg' : code >= 300 ? 'text-info-fg' : 'text-success-fg'
  return <span className={cn('font-mono text-xs font-semibold tabular-nums', tone)}>{code}</span>
}

/** A titled panel with an optional control on the right. */
export function Section({ title, hint, right, children, className, id }: {
  title: string
  hint?: ReactNode
  right?: ReactNode
  children: ReactNode
  className?: string
  id?: string
}) {
  return (
    <section id={id} className={cn('overflow-hidden rounded-lg border border-border bg-surface', className)}>
      <div className="flex flex-wrap items-start justify-between gap-2 border-b border-border px-4 py-2.5">
        <div className="min-w-0">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">{title}</h3>
          {hint && <div className="mt-0.5 text-xs text-text2">{hint}</div>}
        </div>
        {right}
      </div>
      {children}
    </section>
  )
}

/** A right-hand side panel (step or run detail). Escape and the overlay close it. */
export function Drawer({ open, onClose, title, subtitle, children, wide }: {
  open: boolean
  onClose: () => void
  title: ReactNode
  subtitle?: ReactNode
  children: ReactNode
  wide?: boolean
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/50 data-[state=open]:animate-fade-in" />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          className={cn(
            'fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l border-border bg-surface shadow-xl focus:outline-none',
            wide ? 'max-w-[56rem]' : 'max-w-[44rem]',
          )}
        >
          <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
            <div className="min-w-0">
              <DialogPrimitive.Title className="flex min-w-0 items-center gap-2 text-base font-semibold text-text">
                {title}
              </DialogPrimitive.Title>
              {subtitle && <div className="mt-0.5 text-xs text-text2">{subtitle}</div>}
            </div>
            <DialogPrimitive.Close className="rounded p-1 text-text2 hover:bg-surface2 hover:text-text" aria-label="Close">
              <X className="h-4 w-4" aria-hidden />
            </DialogPrimitive.Close>
          </div>
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4">{children}</div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

/** Small label / value pair used in drawers and the configuration panel. */
export function Field({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn('min-w-0', className)}>
      <div className="text-2xs font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-0.5 break-words text-sm text-text">{children}</div>
    </div>
  )
}
