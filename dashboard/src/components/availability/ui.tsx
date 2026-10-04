/**
 * Presentation primitives for the Availability page.
 *
 * Semantic colour roles (theme tokens, light + dark):
 *   met → success · risk → warning · breach → danger · unknown/stale → muted
 *   (slate) · trend/actions → primary · maintenance → accent (violet).
 * Every coloured element is paired with a text label and an icon shape so
 * meaning never depends on colour alone.
 */
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertOctagon, ArrowRight, CheckCircle2, CircleDashed, Info, RefreshCw,
  TriangleAlert, Wrench, XCircle,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { Skeleton } from '@/components/ui/Skeleton'
import type { ReportDataError } from '@/hooks/useReports'
import type { Brief, DecisionMetric, RiskItem, Tone } from './model'

export const TONE: Record<Tone, { text: string; bg: string; border: string; dot: string; fill: string }> = {
  met: { text: 'text-success', bg: 'bg-success/10', border: 'border-success/30', dot: 'bg-success', fill: 'rgb(var(--success))' },
  risk: { text: 'text-warning', bg: 'bg-warning/10', border: 'border-warning/35', dot: 'bg-warning', fill: 'rgb(var(--warning))' },
  breach: { text: 'text-danger', bg: 'bg-danger/10', border: 'border-danger/30', dot: 'bg-danger', fill: 'rgb(var(--danger))' },
  unknown: { text: 'text-muted', bg: 'bg-surface2', border: 'border-border', dot: 'bg-muted', fill: 'rgb(var(--muted))' },
  neutral: { text: 'text-text', bg: 'bg-surface2/70', border: 'border-border', dot: 'bg-text2', fill: 'rgb(var(--text2))' },
  maint: { text: 'text-accent', bg: 'bg-accent/10', border: 'border-accent/30', dot: 'bg-accent', fill: 'rgb(var(--accent))' },
}

export function ToneIcon({ tone, className }: { tone: Tone; className?: string }) {
  const cls = cn('h-4 w-4 shrink-0', className)
  switch (tone) {
    case 'met': return <CheckCircle2 aria-hidden className={cls} />
    case 'risk': return <TriangleAlert aria-hidden className={cls} />
    case 'breach': return <XCircle aria-hidden className={cls} />
    case 'maint': return <Wrench aria-hidden className={cls} />
    case 'unknown': return <CircleDashed aria-hidden className={cls} />
    default: return <Info aria-hidden className={cls} />
  }
}

export function StatusPill({ tone, children, className, title }: { tone: Tone; children: ReactNode; className?: string; title?: string }) {
  const t = TONE[tone]
  return (
    <span
      title={title}
      className={cn('inline-flex items-center gap-1.5 whitespace-nowrap rounded-md border px-2 py-0.5 text-sm font-medium', t.border, t.bg, t.text, className)}
    >
      <ToneIcon tone={tone} className="h-3.5 w-3.5" />
      {children}
    </span>
  )
}

/** Critical → red, warning → amber, info → neutral (never a warning badge). */
export function SeverityPill({ severity }: { severity: string | null | undefined }) {
  const s = (severity || 'info').toLowerCase()
  const tone: Tone = s === 'critical' ? 'breach' : s === 'warning' || s === 'major' ? 'risk' : 'neutral'
  return <StatusPill tone={tone}>{s[0].toUpperCase() + s.slice(1)}</StatusPill>
}

export function Panel({
  title, subtitle, actions, children, className, bodyClassName, id,
}: {
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
  id?: string
}) {
  return (
    <section id={id} className={cn('min-w-0 rounded-xl border border-border bg-surface shadow-card dark:shadow-card-dark', className)}>
      <header className="flex flex-wrap items-start justify-between gap-2 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <h3 className="text-lg font-semibold text-text">{title}</h3>
          {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </header>
      <div className={cn('p-4', bodyClassName)}>{children}</div>
    </section>
  )
}

export function MetricCard({
  metric, loading, dimmed, large,
}: {
  metric?: DecisionMetric
  loading?: boolean
  dimmed?: boolean
  large?: boolean
}) {
  if (loading || !metric) {
    return (
      <div className="rounded-xl border border-border bg-surface p-4" aria-busy="true">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="mt-3 h-9 w-28" />
        <Skeleton className="mt-3 h-4 w-40" />
        <Skeleton className="mt-2 h-4 w-36" />
      </div>
    )
  }
  const valueTone = metric.valueTone && metric.valueTone !== 'neutral' ? TONE[metric.valueTone].text : 'text-text'
  const body = (
    <>
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-base font-medium text-text2" title={metric.definition}>{metric.label}</h3>
        {metric.to && <ArrowRight aria-hidden className="mt-0.5 h-4 w-4 shrink-0 text-muted transition group-hover:translate-x-0.5 group-hover:text-primary" />}
      </div>
      <div className={cn('mt-1 font-semibold tabular-nums tracking-tight', valueTone, large ? 'text-[2.75rem] leading-[3rem]' : 'text-[2rem] leading-10')}>
        {metric.value}
      </div>
      {metric.verdict && (
        <div className="mt-2"><StatusPill tone={metric.verdict.tone}>{metric.verdict.text}</StatusPill></div>
      )}
      <ul className="mt-2 space-y-0.5 text-sm text-muted">
        {metric.lines.map((line) => <li key={line}>{line}</li>)}
      </ul>
      {metric.comparison && (
        <div className={cn('mt-1.5 text-sm tabular-nums', metric.comparison.tone === 'unknown' ? 'text-muted' : TONE[metric.comparison.tone].text)}>
          {metric.comparison.text}
        </div>
      )}
    </>
  )
  const cls = cn(
    'group block rounded-xl border border-border bg-surface p-4 shadow-card transition dark:shadow-card-dark',
    dimmed && 'opacity-60',
    metric.to && 'hover:border-primary/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary',
  )
  return metric.to
    ? <Link to={metric.to} className={cls} aria-label={`${metric.label}: ${metric.value}`}>{body}</Link>
    : <div className={cls}>{body}</div>
}

export function BriefPanel({
  brief, period, dimmed, loading,
}: {
  brief: Brief
  period: string
  dimmed?: boolean
  loading?: boolean
}) {
  const t = TONE[brief.tone]
  return (
    <section
      aria-label="Executive brief"
      className={cn('rounded-xl border bg-surface p-4 shadow-card dark:shadow-card-dark md:px-5', t.border, dimmed && 'opacity-60')}
    >
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <span className={cn('mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg', t.bg, t.text)}>
            <ToneIcon tone={brief.tone} className="h-5 w-5" />
          </span>
          <div className="min-w-0">
            <div className="text-sm text-muted">Executive brief · {period}</div>
            {loading ? (
              <div className="mt-2 space-y-2"><Skeleton className="h-5 w-72" /><Skeleton className="h-4 w-96 max-w-full" /></div>
            ) : (
              <>
                <h2 className="mt-0.5 text-xl font-semibold text-text">{brief.headline}</h2>
                <p className="mt-1 max-w-4xl text-base leading-6 text-text2">{brief.sentences.join(' ')}</p>
                <p className="mt-1 text-sm text-muted">{brief.evidence}</p>
              </>
            )}
          </div>
        </div>
        {brief.action && !loading && (
          <Link
            to={brief.action.to}
            className="inline-flex min-h-[40px] shrink-0 items-center justify-center gap-1.5 rounded-lg border border-border px-3.5 text-sm font-medium text-primary transition hover:border-primary/40 hover:bg-primary/5"
          >
            {brief.action.label}
            <ArrowRight aria-hidden className="h-4 w-4" />
          </Link>
        )}
      </div>
    </section>
  )
}

export function RiskList({ risks, empty, loading }: { risks: RiskItem[]; empty: ReactNode; loading?: boolean }) {
  if (loading) {
    return <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-16 w-full" />)}</div>
  }
  if (!risks.length) return <>{empty}</>
  return (
    <ol className="space-y-2">
      {risks.map((risk, index) => (
        <li key={risk.key}>
          <Link
            to={risk.to}
            className="group flex min-h-[44px] items-start gap-3 rounded-lg border border-border px-3 py-2.5 transition hover:border-primary/40 hover:bg-surface2/50"
          >
            <span className="mt-0.5 w-5 shrink-0 text-sm font-semibold tabular-nums text-muted">{index + 1}</span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill tone={risk.tone}>{risk.status}</StatusPill>
                <span className="truncate text-base font-semibold text-text group-hover:text-primary">{risk.title}</span>
              </div>
              <div className="mt-1 text-sm text-text2">{risk.reason}</div>
              <div className="text-sm text-muted">{risk.context}</div>
            </div>
            <ArrowRight aria-hidden className="mt-1 h-4 w-4 shrink-0 text-muted group-hover:text-primary" />
          </Link>
        </li>
      ))}
    </ol>
  )
}

/**
 * Empty state that says WHY it is empty: a successful query with no findings,
 * missing evidence, or a failed source. Only `no_findings` may reassure.
 */
export function EvidenceEmpty({
  kind, children,
}: {
  kind: 'no_findings' | 'no_evidence' | 'error' | 'partial'
  children: ReactNode
}) {
  const tone: Tone = kind === 'no_findings' ? 'met' : kind === 'error' ? 'breach' : kind === 'partial' ? 'risk' : 'unknown'
  return (
    <div className={cn('flex items-start gap-2 rounded-lg border px-3 py-4 text-base', TONE[tone].border, kind === 'no_findings' ? 'bg-surface' : TONE[tone].bg)}>
      <ToneIcon tone={tone} className={cn('mt-0.5', TONE[tone].text)} />
      <div className={kind === 'no_findings' ? 'text-text2' : TONE[tone].text}>{children}</div>
    </div>
  )
}

export function DataErrorsBanner({ errors }: { errors: ReportDataError[] }) {
  if (!errors.length) return null
  return (
    <div role="status" className="flex items-start gap-2 rounded-lg border border-warning/35 bg-warning/10 px-3 py-2 text-sm text-warning">
      <TriangleAlert aria-hidden className="mt-0.5 h-4 w-4 shrink-0" />
      <div>
        <span className="font-semibold">Partial data.</span>{' '}
        Some sources failed, so empty sections may be incomplete: {errors.map((e) => e.source).join(', ')}.
      </div>
    </div>
  )
}

export function ErrorPanel({ title, message, onRetry }: { title: string; message?: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex flex-col gap-3 rounded-xl border border-danger/30 bg-danger/10 p-4 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-start gap-2 text-danger">
        <AlertOctagon aria-hidden className="mt-0.5 h-5 w-5 shrink-0" />
        <div>
          <div className="text-base font-semibold">{title}</div>
          <div className="text-sm">{message || 'The request failed. No figures are shown rather than guessing.'}</div>
        </div>
      </div>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="inline-flex min-h-[40px] items-center gap-1.5 rounded-lg border border-border bg-surface px-3 text-sm font-medium text-text hover:border-primary/40"
        >
          <RefreshCw aria-hidden className="h-4 w-4" /> Retry
        </button>
      )}
    </div>
  )
}

export interface CountSegment {
  key: string
  label: string
  value: number
  tone: Tone
}

/** Stacked count bar with a text legend (current-state distributions). */
export function CountDistribution({ segments, total, empty = 'No data available' }: { segments: CountSegment[]; total: number; empty?: string }) {
  const shown = segments.filter((s) => s.value > 0)
  if (!shown.length || total <= 0) return <div className="py-4 text-center text-sm text-muted">{empty}</div>
  return (
    <div className="space-y-3">
      <div className="flex h-3 overflow-hidden rounded-full bg-surface2" role="img" aria-label={shown.map((s) => `${s.label} ${s.value}`).join(', ')}>
        {shown.map((s) => (
          <div key={s.key} className={cn('h-full', TONE[s.tone].dot, s.tone === 'unknown' && 'opacity-60')} style={{ width: `${(s.value / total) * 100}%` }} title={`${s.label}: ${s.value}`} />
        ))}
      </div>
      <ul className="grid grid-cols-1 gap-x-4 gap-y-1.5 min-[420px]:grid-cols-2">
        {shown.map((s) => (
          <li key={s.key} className="flex items-center justify-between gap-2 text-sm">
            <span className="flex min-w-0 items-center gap-1.5 text-text2">
              <ToneIcon tone={s.tone} className={cn('h-3.5 w-3.5', TONE[s.tone].text)} />
              <span className="min-w-0 break-words">{s.label}</span>
            </span>
            <span className="font-semibold tabular-nums text-text">{s.value}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Plain table with right-aligned numeric columns and a mobile scroll area. */
export function SimpleTable({
  columns, rows, empty, caption,
}: {
  columns: Array<{ label: string; numeric?: boolean }>
  rows: Array<{ key: string; cells: ReactNode[] }>
  empty: ReactNode
  caption?: string
}) {
  if (!rows.length) return <>{empty}</>
  return (
    <div className="-mx-4 overflow-x-auto">
      <table className="w-full text-base" style={{ minWidth: `${Math.max(240, columns.length * 96)}px` }}>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr className="border-b border-border text-left text-sm text-muted">
            {columns.map((c) => (
              <th key={c.label} scope="col" className={cn('px-4 py-2 font-medium', c.numeric && 'text-right')}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className="border-b border-border/60 last:border-0 hover:bg-surface2/40">
              {row.cells.map((cell, i) => (
                <td key={i} className={cn('px-4 py-2 align-top', columns[i]?.numeric && 'text-right tabular-nums')}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
