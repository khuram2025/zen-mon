import type { ReactNode } from 'react'
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  CircleHelp,
  CircleSlash,
  Info,
  OctagonAlert,
  PowerOff,
  XCircle,
  type LucideIcon,
} from 'lucide-react'
import type { StatusTone } from '@/components/netflow/StatusTile'
import { cn } from '@/lib/utils'
import {
  DETECTOR_STATE,
  SEVERITY_LABEL,
  SEVERITY_TONE,
  STATUS_LABEL,
  type DetectorState,
  type FindingStatus,
  type Severity,
} from './model'

/**
 * Tone chips (NF-29): tinted fill + border + icon + word, text in the `*-fg` tokens that keep
 * ≥ 4.5:1 on the tint in both themes. Never colour alone.
 */
const CHIP: Record<StatusTone, string> = {
  danger: 'border-danger/40 bg-danger/10 text-danger-fg',
  warning: 'border-warning/40 bg-warning/10 text-warning-fg',
  success: 'border-success/40 bg-success/10 text-success-fg',
  info: 'border-info/40 bg-info/10 text-info-fg',
  neutral: 'border-border bg-surface2 text-text2',
  unknown: 'border-border bg-surface2 text-text2',
}

export function ToneChip({
  tone,
  icon: Icon,
  children,
  title,
  className,
}: {
  tone: StatusTone
  icon?: LucideIcon
  children: ReactNode
  title?: string
  className?: string
}) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex max-w-full items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-semibold leading-tight',
        CHIP[tone],
        className,
      )}
    >
      {Icon && <Icon className="h-3 w-3 shrink-0" aria-hidden />}
      <span className="truncate">{children}</span>
    </span>
  )
}

const SEVERITY_ICON: Record<Severity, LucideIcon> = { critical: OctagonAlert, warning: AlertTriangle, info: Info }

export function SeverityChip({ severity, className }: { severity: Severity; className?: string }) {
  return (
    <ToneChip tone={SEVERITY_TONE[severity] ?? 'neutral'} icon={SEVERITY_ICON[severity] ?? Info} className={className}>
      {SEVERITY_LABEL[severity] ?? severity}
    </ToneChip>
  )
}

const DETECTOR_ICON: Record<DetectorState, LucideIcon> = {
  ok: CheckCircle2,
  unavailable: CircleSlash,
  no_coverage: CircleHelp,
  failed: XCircle,
  disabled: PowerOff,
}

export function DetectorStateChip({ state, className }: { state: DetectorState; className?: string }) {
  const meta = DETECTOR_STATE[state] ?? DETECTOR_STATE.failed
  return (
    <ToneChip tone={meta.tone} icon={DETECTOR_ICON[state] ?? XCircle} title={meta.hint} className={className}>
      {meta.label}
    </ToneChip>
  )
}

const STATUS_TONE: Record<FindingStatus, StatusTone> = {
  open: 'warning',
  acknowledged: 'info',
  suppressed: 'neutral',
  resolved: 'success',
  candidate: 'neutral',
}

export function FindingStatusChip({ status, className }: { status: FindingStatus; className?: string }) {
  return (
    <ToneChip tone={STATUS_TONE[status] ?? 'neutral'} icon={status === 'suppressed' ? Ban : undefined} className={className}>
      {STATUS_LABEL[status] ?? status}
    </ToneChip>
  )
}

/** A small labelled value for detail grids (never truncates the value). */
export function Field({ label, children, mono }: { label: string; children: ReactNode; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] font-medium text-text2">{label}</dt>
      <dd className={cn('break-words text-sm text-text', mono && 'font-mono text-[13px]')}>{children}</dd>
    </div>
  )
}
