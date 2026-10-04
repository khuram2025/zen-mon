import { CheckCircle2, CircleHelp, CircleSlash, Hourglass } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { NfExporterState } from '@/lib/netflow/types'

/**
 * Export receipt state as the appliance computes it (`/netflow/exporters/status`). The words
 * describe what the collector observes — flows arriving, late or silent — and never claim the
 * device itself is down: exporter silence is not reachability evidence (NF-23).
 */
const STATES: Record<NfExporterState, { word: string; cls: string; icon: typeof CheckCircle2; hint: string }> = {
  receiving: {
    word: 'Receiving',
    cls: 'border-success/35 bg-success/10 text-success-fg',
    icon: CheckCircle2,
    hint: 'Flows arrived within the expected export interval',
  },
  late: {
    word: 'Late',
    cls: 'border-warning/35 bg-warning/10 text-warning-fg',
    icon: Hourglass,
    hint: 'No flows for longer than the expected export interval',
  },
  silent: {
    word: 'Silent',
    cls: 'border-danger/35 bg-danger/10 text-danger-fg',
    icon: CircleSlash,
    hint: 'No flows received for a long time; the device may still be reachable',
  },
  never: {
    word: 'Never received',
    cls: 'border-border bg-surface2 text-text2',
    icon: CircleHelp,
    hint: 'The collector has no record of flows from this exporter',
  },
}

export function ExporterStateBadge({ state, className }: { state: NfExporterState; className?: string }) {
  const s = STATES[state] ?? STATES.never
  const Icon = s.icon
  return (
    <span
      className={cn('inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-semibold', s.cls, className)}
      title={s.hint}
    >
      <Icon className="h-3 w-3" aria-hidden />
      {s.word}
    </span>
  )
}
